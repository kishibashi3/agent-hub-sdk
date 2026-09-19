package agenthub_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	agenthub "github.com/kishibashi3/agent-hub-sdk/go"
)

// ──────────────────────────────────────────────────────────────────────── //
// New() validation                                                         //
// ──────────────────────────────────────────────────────────────────────── //

func TestNew_missingEndpoint(t *testing.T) {
	_, err := agenthub.New("", "pat", "user", "")
	if err == nil {
		t.Fatal("want error for empty endpoint")
	}
}

func TestNew_missingPAT(t *testing.T) {
	_, err := agenthub.New("http://localhost", "", "user", "")
	if err == nil {
		t.Fatal("want error for empty pat")
	}
}

func TestNew_missingUserID(t *testing.T) {
	_, err := agenthub.New("http://localhost", "pat", "", "")
	if err == nil {
		t.Fatal("want error for empty userID")
	}
}

func TestNew_valid(t *testing.T) {
	c, err := agenthub.New("http://localhost:3000", "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if c == nil {
		t.Fatal("want non-nil client")
	}
}

func TestNew_withClientName(t *testing.T) {
	c, err := agenthub.New(
		"http://localhost:3000", "ghp_test", "bridge-test", "",
		agenthub.WithClientName("my-bridge"),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if c == nil {
		t.Fatal("want non-nil client")
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// readFirstSSEData (via exported helper for white-box testing)             //
// readFirstSSEData は unexported なので SSE レスポンスを返す httptest server
// 経由でテストする代わりに、入出力パターンを直接テストする。
// ──────────────────────────────────────────────────────────────────────── //

func TestReadFirstSSEData_viaParseMessages(t *testing.T) {
	// readFirstSSEData は GetMessages の内部で呼ばれる。
	// 単体テストは httptest.Server が必要なので integration test 扱い。
	// ここでは SSE パーサが使うデータ形式が ParseMessages で正しく解釈される
	// ことを確認するだけにとどめる (SSE → ParseMessages の結合部分)。
	ssePayload := `[{"id":"sse1","from":"@alice","to":"@bot","message":"via SSE"}]`
	msgs, err := agenthub.ParseMessages(ssePayload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(msgs) != 1 || msgs[0].ID != "sse1" {
		t.Errorf("unexpected parse result: %+v", msgs)
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// rpc.Result null check (regression)                                       //
// ──────────────────────────────────────────────────────────────────────── //

func TestNew_clientName_defaultValue(t *testing.T) {
	// WithClientName オプションなしでは "agent-hub-sdk-go" が使われる。
	// 直接フィールドにアクセスできないため、エラーなく生成されることだけ確認。
	c, err := agenthub.New("http://localhost", "pat", "user", "tenant")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	_ = c
}

func TestParseMessages_wireFieldMapping(t *testing.T) {
	// "from" wire field → Sender、"message" wire field → Body のマッピング確認
	input := `[{"id":"x1","from":"@sender","to":"@target","message":"body-text"}]`
	msgs, _ := agenthub.ParseMessages(input)
	if len(msgs) != 1 {
		t.Fatalf("want 1, got %d", len(msgs))
	}
	if msgs[0].Sender != "@sender" {
		t.Errorf("Sender: want @sender, got %q", msgs[0].Sender)
	}
	if msgs[0].Body != "body-text" {
		t.Errorf("Body: want body-text, got %q", msgs[0].Body)
	}
	if !strings.HasPrefix(msgs[0].ID, "x") {
		t.Errorf("ID: unexpected %q", msgs[0].ID)
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// SubscribeInbox / OnInboxPush (issue #46)                                //
// ──────────────────────────────────────────────────────────────────────── //

// newTestServer は MCP ハンドシェイク + resources/subscribe + GET SSE を処理する
// テスト用 httptest.Server を生成する。
// sseCh に送った行は SSE data イベントとして SSE 接続に流れる。
func newTestServer(t *testing.T, sseCh <-chan string) *httptest.Server {
	t.Helper()
	var reqCount atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")

		if r.Method == http.MethodGet {
			// SSE long-lived stream
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Cache-Control", "no-cache")
			w.WriteHeader(http.StatusOK)
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
			for line := range sseCh {
				fmt.Fprintf(w, "data: %s\n\n", line)
				if f, ok := w.(http.Flusher); ok {
					f.Flush()
				}
			}
			return
		}

		// POST: parse method and respond
		var req struct {
			Method string `json:"method"`
			ID     *int64 `json:"id"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)

		n := reqCount.Add(1)
		switch req.Method {
		case "initialize":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"test","version":"0.0.1"}}}`, n)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "resources/subscribe":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{}}`, n)
		default:
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{}}`, n)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestSubscribeInbox_sendsResourcesSubscribe(t *testing.T) {
	sseCh := make(chan string)
	close(sseCh) // SSE stream が即終了 (subscribe のみテスト)

	srv := newTestServer(t, sseCh)
	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}

	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	if err := c.SubscribeInbox(ctx); err != nil {
		t.Fatalf("SubscribeInbox: %v", err)
	}
}

func TestOnInboxPush_callbackFiredOnSSEPush(t *testing.T) {
	sseCh := make(chan string, 1)
	srv := newTestServer(t, sseCh)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}

	var fired atomic.Bool
	done := make(chan struct{})
	c.OnInboxPush(func() {
		if fired.CompareAndSwap(false, true) {
			close(done)
		}
	})

	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	if err := c.StartSSE(ctx); err != nil {
		t.Fatalf("StartSSE: %v", err)
	}
	t.Cleanup(c.StopSSE)

	// SSE ストリームに notifications/resources/updated を流す
	sseCh <- `{"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":"inbox://@bridge-test"}}`
	close(sseCh)

	select {
	case <-done:
		// OK: コールバックが呼ばれた
	case <-time.After(3 * time.Second):
		t.Fatal("timeout: OnInboxPush callback was not called")
	}
}

func TestOnInboxPush_noCallbackNoPanic(t *testing.T) {
	// コールバック未登録で notifications/resources/updated が来ても panic しない
	sseCh := make(chan string, 1)
	srv := newTestServer(t, sseCh)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}

	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	if err := c.StartSSE(ctx); err != nil {
		t.Fatalf("StartSSE: %v", err)
	}
	t.Cleanup(c.StopSSE)

	sseCh <- `{"jsonrpc":"2.0","method":"notifications/resources/updated","params":{}}`
	close(sseCh)

	// 短時間待って panic がないことを確認
	time.Sleep(200 * time.Millisecond)
}

// ──────────────────────────────────────────────────────────────────────── //
// SSE 行長: 上限なしで読み切れること (issue #60)                           //
// ──────────────────────────────────────────────────────────────────────── //

// newBigGetMessagesServer は get_messages に対して body が bodyBytes バイトの
// メッセージ 1 件を SSE (1 行の data) で返すテストサーバを生成する。
func newBigGetMessagesServer(t *testing.T, bodyBytes int) *httptest.Server {
	t.Helper()
	var reqCount atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")

		var req struct {
			Method string `json:"method"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		n := reqCount.Add(1)

		switch req.Method {
		case "initialize":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"test","version":"0.0.1"}}}`, n)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		default:
			payload, err := json.Marshal([]map[string]string{{
				"id":      "big1",
				"from":    "@alice",
				"to":      "@bridge-test",
				"message": strings.Repeat("x", bodyBytes),
			}})
			if err != nil {
				t.Error(err)
				return
			}
			inner, err := json.Marshal(string(payload))
			if err != nil {
				t.Error(err)
				return
			}
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"content\":[{\"type\":\"text\",\"text\":%s}]}}\n\n", n, inner)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func fetchOneBigMessage(t *testing.T, bodyBytes int) agenthub.Message {
	t.Helper()
	srv := newBigGetMessagesServer(t, bodyBytes)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	msgs, err := c.GetMessages(ctx)
	if err != nil {
		t.Fatalf("GetMessages: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("want 1 message, got %d", len(msgs))
	}
	return msgs[0]
}

// 旧実装 (bufio.Scanner + 128 KiB 固定上限) では "token too long" で恒久的に
// 受信不能になっていたサイズが読めること。
func TestGetMessages_largeResponseAboveOldScannerLimit(t *testing.T) {
	const bodyBytes = 512 * 1024 // 旧上限 128 KiB の 4 倍
	msg := fetchOneBigMessage(t, bodyBytes)
	if len(msg.Body) != bodyBytes {
		t.Errorf("body length: want %d, got %d", bodyBytes, len(msg.Body))
	}
}

// WARN 閾値を超えた行も読み切れること + 受信サイズが数値で WARN に出ること。
// 上限を外した以上「巨大化していること自体に誰も気づかない」を防ぐのが目的。
func TestGetMessages_oversizedLineIsReadAndWarned(t *testing.T) {
	const bodyBytes = 9 << 20 // WARN 閾値 (8 MiB) 超え

	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn})))
	t.Cleanup(func() { slog.SetDefault(prev) })

	msg := fetchOneBigMessage(t, bodyBytes)
	if len(msg.Body) != bodyBytes {
		t.Errorf("body length: want %d, got %d", bodyBytes, len(msg.Body))
	}

	logged := buf.String()
	if !strings.Contains(logged, "level=WARN") || !strings.Contains(logged, "oversized line received") {
		t.Fatalf("want a WARN about the oversized line, got:\n%s", logged)
	}
	// 受信サイズが数値で出ていること (行全体なので body より必ず大きい)
	m := regexp.MustCompile(`bytes=(\d+)`).FindStringSubmatch(logged)
	if m == nil {
		t.Fatalf("WARN should report the received size as a number, got:\n%s", logged)
	}
	n, err := strconv.Atoi(m[1])
	if err != nil {
		t.Fatal(err)
	}
	if n < bodyBytes {
		t.Errorf("reported size %d should be >= body size %d", n, bodyBytes)
	}
}

// 閾値以下の通常サイズでは WARN を出さない (ログを汚さない)。
func TestGetMessages_normalSizeIsNotWarned(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn})))
	t.Cleanup(func() { slog.SetDefault(prev) })

	fetchOneBigMessage(t, 1024)

	if strings.Contains(buf.String(), "oversized line") {
		t.Errorf("unexpected WARN for a normal-sized response:\n%s", buf.String())
	}
}

// 最終行が改行で終わらずに EOF になっても、その行を取りこぼさないこと。
// bufio.Reader.ReadString は最後の断片を err=io.EOF と一緒に返すため、
// 「err != nil なら捨てる」実装だとレスポンスを丸ごと落とす。
func TestGetMessages_lastLineWithoutTrailingNewline(t *testing.T) {
	var reqCount atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")
		var req struct {
			Method string `json:"method"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		n := reqCount.Add(1)
		switch req.Method {
		case "initialize":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"test","version":"0.0.1"}}}`, n)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		default:
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			// 末尾に改行なし
			fmt.Fprintf(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"[{\\\"id\\\":\\\"nl1\\\",\\\"from\\\":\\\"@alice\\\",\\\"to\\\":\\\"@bridge-test\\\",\\\"message\\\":\\\"no trailing newline\\\"}]\"}]}}", n)
		}
	}))
	t.Cleanup(srv.Close)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	msgs, err := c.GetMessages(ctx)
	if err != nil {
		t.Fatalf("GetMessages: %v", err)
	}
	if len(msgs) != 1 || msgs[0].ID != "nl1" {
		t.Fatalf("unexpected messages: %+v", msgs)
	}
}

// 常時 SSE ストリーム側 (runSSELoop) も同じ地雷を踏まないこと:
// 旧上限を超える大きさの 1 イベントでも push コールバックが発火する。
func TestSSEStream_largeEventStillDispatches(t *testing.T) {
	sseCh := make(chan string, 1)
	srv := newTestServer(t, sseCh)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var fired atomic.Bool
	c.OnInboxPush(func() {
		if fired.CompareAndSwap(false, true) {
			close(done)
		}
	})

	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	if err := c.StartSSE(ctx); err != nil {
		t.Fatalf("StartSSE: %v", err)
	}
	t.Cleanup(c.StopSSE)

	// 旧上限 (128 KiB) を大きく超える 1 行のイベント
	padding := strings.Repeat("y", 512*1024)
	sseCh <- fmt.Sprintf(`{"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":"inbox://@bridge-test","pad":"%s"}}`, padding)
	close(sseCh)

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("timeout: large SSE event did not reach the callback")
	}
}
