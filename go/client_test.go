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
	"sync"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"

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
	return newVariableGetMessagesServer(t, func() int { return bodyBytes })
}

// newVariableGetMessagesServer は newBigGetMessagesServer と同じだが、
// レスポンスの body サイズを呼び出しごとに bodySize() で決める。
// 「同じ大きさの未読が滞留している間の poll 連打」を再現するのに使う。
func newVariableGetMessagesServer(t *testing.T, bodySize func() int) *httptest.Server {
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
				"message": strings.Repeat("x", bodySize()),
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

func fetchOneBigMessage(t *testing.T, bodyBytes int, opts ...agenthub.ClientOption) agenthub.Message {
	t.Helper()
	srv := newBigGetMessagesServer(t, bodyBytes)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "", opts...)
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
	const bodyBytes = 2 << 20 // WARN 閾値 (1 MiB) 超え

	var buf bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn}))

	msg := fetchOneBigMessage(t, bodyBytes, agenthub.WithLogger(logger))
	if len(msg.Body) != bodyBytes {
		t.Errorf("body length: want %d, got %d", bodyBytes, len(msg.Body))
	}

	logged := buf.String()
	if !strings.Contains(logged, "level=WARN") || !strings.Contains(logged, "oversized line received") {
		t.Fatalf("want a WARN about the oversized line, got:\n%s", logged)
	}
	// 埋め込み側 (bridge 等) も自前の oversize WARN を出すため、ログを文言で grep した
	// ときに SDK 由来だと判別できる識別子を必ず含める。
	if !strings.Contains(logged, "[agent-hub-sdk]") {
		t.Errorf("WARN should identify the SDK as its source, got:\n%s", logged)
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
	logger := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn}))

	fetchOneBigMessage(t, 1024, agenthub.WithLogger(logger))

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

// ──────────────────────────────────────────────────────────────────────── //
// 壊れた巨大 payload: エラー文字列が truncate されること (issue #64)        //
// ──────────────────────────────────────────────────────────────────────── //

// newBrokenGetMessagesServer は get_messages に対して JSON として壊れた
// brokenBytes バイトのレスポンスを返すテストサーバを生成する。
func newBrokenGetMessagesServer(t *testing.T, brokenBytes int) *httptest.Server {
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
			// 途中で切れた JSON (閉じ括弧なし) を 1 行の SSE data として返す。
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"%s\n\n", n, strings.Repeat("x", brokenBytes))
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// 旧実装は raw payload 全体を %q で抱えていたため、数 MiB のレスポンスが
// そのまま (エスケープで膨らんだうえで) エラー文字列になっていた。
func TestGetMessages_brokenLargePayload_errorIsTruncated(t *testing.T) {
	const brokenBytes = 4 << 20 // 4 MiB
	srv := newBrokenGetMessagesServer(t, brokenBytes)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}

	_, err = c.GetMessages(ctx)
	if err == nil {
		t.Fatal("want unmarshal error for broken payload")
	}

	msg := err.Error()
	// 全長は数値として残っていること (切り分けに必要)。
	// 切り詰め後の長さ (2048) を報告する退行を落とすため、実際の値まで検証する。
	m := regexp.MustCompile(`raw (\d+) bytes`).FindStringSubmatch(msg)
	if m == nil {
		t.Fatalf("error message lacks raw size info: %q", msg)
	}
	reported, convErr := strconv.Atoi(m[1])
	if convErr != nil {
		t.Fatalf("raw size is not a number: %q", m[1])
	}
	// payload は SSE/JSON の prefix を含むので brokenBytes 以上になる。
	if reported < brokenBytes {
		t.Errorf("raw size %d is not the full payload length (want >= %d): %q", reported, brokenBytes, msg)
	}
	// truncation marker は "first N: <quoted snippet>"。N は errRawSnippetBytes だが
	// 定数の値には依存させず、N と実際に埋め込まれた snippet 長の整合で検証する。
	tm := truncMarkerRe.FindStringSubmatch(msg)
	if tm == nil {
		t.Fatalf("error message lacks truncation marker: %q", msg)
	}
	limit, convErr := strconv.Atoi(tm[1])
	if convErr != nil {
		t.Fatalf("snippet limit is not a number: %q", tm[1])
	}
	if limit <= 0 || limit >= reported {
		t.Errorf("snippet limit %d is not a truncation of %d bytes", limit, reported)
	}
	snippet, unqErr := strconv.Unquote(tm[2])
	if unqErr != nil {
		t.Fatalf("snippet is not a valid quoted string: %v", unqErr)
	}
	if len(snippet) != limit {
		t.Errorf("snippet length = %d, want %d (as reported by marker)", len(snippet), limit)
	}
	// snippet 以外の付随テキストは小さいこと (payload 全体を抱えていないこと)。
	if overhead := len(msg) - len(tm[2]); overhead > 1024 {
		t.Errorf("error message not truncated: %d bytes besides snippet (payload was %d bytes)", overhead, brokenBytes)
	}
}

// truncMarkerRe は rawSnippet の truncation marker "first N: <%q snippet>" を拾う。
var truncMarkerRe = regexp.MustCompile(`first (\d+): ("(?:[^"\\]|\\.)*")`)

// 小さい payload はそのまま全部出る (truncate しない)。
func TestGetMessages_brokenSmallPayload_errorKeepsFullRaw(t *testing.T) {
	const brokenBytes = 32
	srv := newBrokenGetMessagesServer(t, brokenBytes)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	ctx := t.Context()
	if err := c.Initialize(ctx); err != nil {
		t.Fatalf("Initialize: %v", err)
	}

	_, err = c.GetMessages(ctx)
	if err == nil {
		t.Fatal("want unmarshal error for broken payload")
	}
	msg := err.Error()
	if truncMarkerRe.MatchString(msg) {
		t.Errorf("small payload should not be truncated: %q", msg)
	}
	if !strings.Contains(msg, strings.Repeat("x", brokenBytes)) {
		t.Errorf("small payload should appear in full: %q", msg)
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// runSSELoop: 終端時の flush (issue #63)                                   //
// ──────────────────────────────────────────────────────────────────────── //

// sseStreamServer は GET /mcp で writeSSE の内容だけを流して閉じるサーバ。
// POST は newTestServer と同じ最小の MCP 応答を返す。
func sseStreamServer(t *testing.T, writeSSE func(w http.ResponseWriter)) *httptest.Server {
	t.Helper()
	var reqCount atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")
		if r.Method == http.MethodGet {
			// ヘッダは writeSSE 側で書く (Hijack するケースがあるため)
			writeSSE(w)
			return
		}
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
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{}}`, n)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func writeSSEHeaders(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
}

// startSSEWithCallback は SSE を開始し、inbox push が来たら閉じる channel を返す。
func startSSEWithCallback(t *testing.T, srv *httptest.Server) <-chan struct{} {
	t.Helper()
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
	return done
}

const inboxNotification = `{"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":"inbox://@bridge-test"}}`

// blank line 無しで EOF 終端したストリームでも、溜まっている data: 行を取りこぼさないこと。
// 修正前は readErr == io.EOF の時点で dataLines ごと捨てていたため発火しない。
func TestSSEStream_flushesPendingEventOnEOF(t *testing.T) {
	srv := sseStreamServer(t, func(w http.ResponseWriter) {
		writeSSEHeaders(w)
		// 末尾の blank line 無しで接続終了
		fmt.Fprintf(w, "event: message\ndata: %s\n", inboxNotification)
	})

	select {
	case <-startSSEWithCallback(t, srv):
	case <-time.After(5 * time.Second):
		t.Fatal("timeout: event terminated by EOF (no blank line) did not reach the callback")
	}
}

// 行の途中で EOF した場合 (data: 行が改行で終わらない) も、その断片を含めて flush すること。
func TestSSEStream_flushesPartialLineOnEOF(t *testing.T) {
	srv := sseStreamServer(t, func(w http.ResponseWriter) {
		writeSSEHeaders(w)
		// 改行すら無しで接続終了
		fmt.Fprintf(w, "event: message\ndata: %s", inboxNotification)
	})

	select {
	case <-startSSEWithCallback(t, srv):
	case <-time.After(5 * time.Second):
		t.Fatal("timeout: event terminated mid-line did not reach the callback")
	}
}

// 非 EOF の read エラー (chunked 応答が終端チャンク無しで切れる = io.ErrUnexpectedEOF)
// でも、行として完結済みの data: は捨てないこと。再送されないため落とすと通知が消える。
func TestSSEStream_flushesPendingEventOnUnexpectedEOF(t *testing.T) {
	srv := sseStreamServer(t, func(w http.ResponseWriter) {
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Error("ResponseWriter does not support hijacking")
			return
		}
		conn, bw, err := hj.Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		// 終端チャンク (0\r\n\r\n) を書かずに切る → client 側は io.ErrUnexpectedEOF
		fmt.Fprint(bw, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nmcp-session-id: test-session-id\r\nTransfer-Encoding: chunked\r\n\r\n")
		chunk := fmt.Sprintf("event: message\ndata: %s\n", inboxNotification)
		fmt.Fprintf(bw, "%x\r\n%s\r\n", len(chunk), chunk)
		_ = bw.Flush()
		_ = conn.Close()
	})

	select {
	case <-startSSEWithCallback(t, srv):
	case <-time.After(5 * time.Second):
		t.Fatal("timeout: event followed by an unexpected EOF did not reach the callback")
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// oversized line WARN のレート制限 / WithLogger (issue #65)                //
// ──────────────────────────────────────────────────────────────────────── //

// newWarnCountingClient は WARN を buf に集める client と、poll のたびの body サイズを
// 決める関数を組み合わせて返す。
func newWarnCountingClient(t *testing.T, buf *bytes.Buffer, bodySize func() int) *agenthub.Client {
	t.Helper()
	srv := newVariableGetMessagesServer(t, bodySize)
	logger := slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelWarn}))
	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "", agenthub.WithLogger(logger))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Initialize(t.Context()); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	return c
}

func countOversizedWarns(logged string) int {
	return strings.Count(logged, "oversized line received")
}

// 未読が閾値を超えたまま滞留している間、poll を繰り返しても WARN は 1 回だけ。
// readFirstSSEData は RPC ごとに新しい reader を作るため、レート制限が無いと
// poll のたびに鳴り続ける (bridge の safety-net poll 30 秒で 2,880 行/日)。
func TestOversizedWarn_isRateLimitedAcrossPolls(t *testing.T) {
	var buf bytes.Buffer
	c := newWarnCountingClient(t, &buf, func() int { return 2 << 20 }) // 閾値 1 MiB 超え

	for i := 0; i < 5; i++ {
		if _, err := c.GetMessages(t.Context()); err != nil {
			t.Fatalf("GetMessages #%d: %v", i, err)
		}
	}

	if got := countOversizedWarns(buf.String()); got != 1 {
		t.Errorf("want exactly 1 WARN for a stable backlog across 5 polls, got %d:\n%s", got, buf.String())
	}
}

// 「滞留」は黙るが「悪化」(サイズが 2 倍以上) は鳴る。
func TestOversizedWarn_firesAgainWhenSizeDoubles(t *testing.T) {
	var buf bytes.Buffer
	var size atomic.Int64
	size.Store(2 << 20)
	c := newWarnCountingClient(t, &buf, func() int { return int(size.Load()) })

	if _, err := c.GetMessages(t.Context()); err != nil {
		t.Fatal(err)
	}
	if got := countOversizedWarns(buf.String()); got != 1 {
		t.Fatalf("want 1 WARN after the first oversized poll, got %d", got)
	}

	// わずかな増加では鳴らない
	size.Store(2<<20 + 1024)
	if _, err := c.GetMessages(t.Context()); err != nil {
		t.Fatal(err)
	}
	if got := countOversizedWarns(buf.String()); got != 1 {
		t.Errorf("a small increase should stay silent, got %d WARNs:\n%s", got, buf.String())
	}

	// 2 倍を超えたら鳴る
	size.Store(6 << 20)
	if _, err := c.GetMessages(t.Context()); err != nil {
		t.Fatal(err)
	}
	if got := countOversizedWarns(buf.String()); got != 2 {
		t.Errorf("want a second WARN once the line size doubled, got %d:\n%s", got, buf.String())
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// GetThread (issue #81)                                                   //
// ──────────────────────────────────────────────────────────────────────── //

// newGetThreadServer は tools/call に対して toolText を text content として返し、
// 受け取った arguments を gotArgs に記録するテストサーバを生成する。
func newGetThreadServer(t *testing.T, toolText string, gotArgs *map[string]any) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")
		var req struct {
			Method string `json:"method"`
			ID     *int64 `json:"id"`
			Params struct {
				Name      string         `json:"name"`
				Arguments map[string]any `json:"arguments"`
			} `json:"params"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		w.Header().Set("Content-Type", "application/json")
		switch req.Method {
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "tools/call":
			if req.Params.Name != "get_thread" {
				t.Errorf("tool name = %q, want get_thread", req.Params.Name)
			}
			*gotArgs = req.Params.Arguments
			text, _ := json.Marshal(toolText)
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":%s}]}}`, *req.ID, text)
		default:
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{}}`, *req.ID)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func getThreadVia(t *testing.T, toolText string, limit int) (*agenthub.Thread, map[string]any, error) {
	t.Helper()
	var gotArgs map[string]any
	srv := newGetThreadServer(t, toolText, &gotArgs)
	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Initialize(t.Context()); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	th, err := c.GetThread(t.Context(), "msg-c", limit)
	return th, gotArgs, err
}

func TestGetThread_parsesThread(t *testing.T) {
	const text = `{
  "root_id": "msg-a",
  "thread_size": 3,
  "messages": [
    {"id":"msg-a","from":"@alice","to":"@bob","message":"A","caused_by":null,"timestamp":"2026-10-17T00:00:00.000Z"},
    {"id":"msg-b","from":"@bob","to":"@alice","message":"B","caused_by":"msg-a","timestamp":"2026-10-17T00:00:01.000Z"},
    {"id":"msg-c","from":"@alice","to":"@bob","message":"C","caused_by":"msg-b","timestamp":"2026-10-17T00:00:02.000Z"}
  ]
}`
	th, args, err := getThreadVia(t, text, 0)
	if err != nil {
		t.Fatalf("GetThread: %v", err)
	}
	if args["message_id"] != "msg-c" {
		t.Errorf("message_id = %v, want msg-c", args["message_id"])
	}
	if _, ok := args["limit"]; ok {
		t.Errorf("limit should be omitted when 0, got %v", args["limit"])
	}
	if th.RootID != "msg-a" || th.ThreadSize != 3 || len(th.Messages) != 3 {
		t.Fatalf("unexpected thread: %+v", th)
	}
	want := []struct{ id, from, causedBy string }{
		{"msg-a", "@alice", ""}, {"msg-b", "@bob", "msg-a"}, {"msg-c", "@alice", "msg-b"},
	}
	for i, w := range want {
		m := th.Messages[i]
		if m.ID != w.id || m.Sender != w.from || m.CausedBy != w.causedBy {
			t.Errorf("messages[%d] = %+v, want id=%s from=%s caused_by=%q", i, m, w.id, w.from, w.causedBy)
		}
	}
}

func TestGetThread_sendsLimit(t *testing.T) {
	_, args, err := getThreadVia(t, `{"root_id":"msg-a","thread_size":1,"messages":[]}`, 2)
	if err != nil {
		t.Fatalf("GetThread: %v", err)
	}
	if args["limit"] != float64(2) {
		t.Errorf("limit = %v, want 2", args["limit"])
	}
}

// server は権限なし等を isError ではなく {"error": ...} の成功テキストで返す。
// 空の成功として扱わず error にすること。
func TestGetThread_serverErrorTextIsError(t *testing.T) {
	th, _, err := getThreadVia(t, `{"error":"スレッド msg-a を閲覧する権限がありません"}`, 0)
	if err == nil {
		t.Fatalf("want error, got thread %+v", th)
	}
	if !strings.Contains(err.Error(), "閲覧する権限がありません") {
		t.Errorf("error should carry server message, got: %v", err)
	}
}

func TestParseThread_missingRootIDIsError(t *testing.T) {
	if _, err := agenthub.ParseThread(`{}`); err == nil {
		t.Fatal("want error for response without root_id")
	}
	if _, err := agenthub.ParseThread(`not json`); err == nil {
		t.Fatal("want error for non-JSON text")
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// HTTP エラー body の上限 (issue #70)                                      //
// ──────────────────────────────────────────────────────────────────────── //

func TestErrBodySnippet_truncatesLargeBody(t *testing.T) {
	const limit = 2048
	got := agenthub.ErrBodySnippet(strings.NewReader(strings.Repeat("A", 512*1024)))
	if !strings.Contains(got, "truncated at") {
		t.Errorf("want a truncation marker, got %q…", got[:min(len(got), 80)])
	}
	if len(got) > limit+64 { // 切り詰め本体 + マーカー分の余裕
		t.Errorf("snippet length %d is not bounded by the %d-byte limit", len(got), limit)
	}
}

// 切り詰めはバイト単位なのでマルチバイト rune を割る。割れた端切れを載せない。
func TestErrBodySnippet_truncatedSnippetIsValidUTF8(t *testing.T) {
	got := agenthub.ErrBodySnippet(strings.NewReader(strings.Repeat("あ", 1000)))
	if !strings.Contains(got, "truncated at") {
		t.Fatalf("want a truncation marker, got %q…", got[:min(len(got), 80)])
	}
	if !utf8.ValidString(got) {
		t.Errorf("truncated snippet is not valid UTF-8: %q…", got[:min(len(got), 80)])
	}
}

func TestErrBodySnippet_keepsSmallBodyVerbatim(t *testing.T) {
	const body = `{"error":"participant not found"}`
	if got := agenthub.ErrBodySnippet(strings.NewReader(body)); got != body {
		t.Errorf("want the body verbatim, got %q", got)
	}
	if got := agenthub.ErrBodySnippet(strings.NewReader("  " + body + "\n")); got != body {
		t.Errorf("want surrounding whitespace trimmed, got %q", got)
	}
}

// postRPC (client.go:420) — LB が返す巨大な HTML エラーでエラー文字列が膨らまないこと。
func TestPostRPC_errorBodyIsTruncated(t *testing.T) {
	huge := strings.Repeat("<html>502 Bad Gateway</html>", 32*1024) // ≒ 900 KiB
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")
		var req struct {
			Method string `json:"method"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req.Method == "initialize" {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"test","version":"0.0.1"}}}`)
			return
		}
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprint(w, huge)
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
	_, err = c.GetMessages(ctx)
	if err == nil {
		t.Fatal("want an error for HTTP 502")
	}
	msg := err.Error()
	if !strings.Contains(msg, "HTTP 502") {
		t.Errorf("want the status code in the error, got %q", msg[:min(len(msg), 120)])
	}
	if len(msg) > 4096 {
		t.Errorf("error string length %d — the %d KiB body was not truncated", len(msg), len(huge)/1024)
	}
}

// 小さいエラー body は従来どおり全文が出ること (回帰防止)。
func TestPostRPC_smallErrorBodyIsKept(t *testing.T) {
	const detail = `{"error":"tenant not found"}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")
		var req struct {
			Method string `json:"method"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req.Method == "initialize" {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"test","version":"0.0.1"}}}`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, detail)
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
	if _, err := c.GetMessages(ctx); err == nil || !strings.Contains(err.Error(), detail) {
		t.Fatalf("want the full body in the error, got %v", err)
	}
}

// runSSELoop (client.go:529) — SSE GET のエラー応答も同じく切り詰めること。
// handleSSEStream は runSSELoop のエラーを握り潰すため、白箱フックで直接叩く。
func TestRunSSELoop_errorBodyIsTruncated(t *testing.T) {
	huge := strings.Repeat("<html>503 Service Unavailable</html>", 32*1024)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, huge)
	}))
	t.Cleanup(srv.Close)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	err = c.RunSSELoopForTest(t.Context(), "test-session-id")
	if err == nil {
		t.Fatal("want an error for SSE GET HTTP 503")
	}
	msg := err.Error()
	if !strings.Contains(msg, "SSE GET HTTP 503") {
		t.Errorf("want the status code in the error, got %q", msg[:min(len(msg), 120)])
	}
	if len(msg) > 4096 {
		t.Errorf("error string length %d — the %d KiB body was not truncated", len(msg), len(huge)/1024)
	}
}

// 小さい SSE GET エラー body は全文が出ること。
func TestRunSSELoop_smallErrorBodyIsKept(t *testing.T) {
	const detail = `{"error":"session not found"}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, detail)
	}))
	t.Cleanup(srv.Close)

	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
	if err != nil {
		t.Fatal(err)
	}
	err = c.RunSSELoopForTest(t.Context(), "test-session-id")
	if err == nil || !strings.Contains(err.Error(), detail) {
		t.Fatalf("want the full body in the error, got %v", err)
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// SSE unmarshal 失敗時の Debug ログ (issue #79)                            //
// ──────────────────────────────────────────────────────────────────────── //

// logSink は SSE goroutine が書く slog 出力を受け取る io.Writer。
// bytes.Buffer は goroutine 安全ではないため mutex で包み、want を含む行が
// 書かれた時点で ready を閉じてテスト側の待ち合わせに使う。
type logSink struct {
	mu    sync.Mutex
	buf   bytes.Buffer
	want  string
	once  sync.Once
	ready chan struct{}
}

func newLogSink(want string) *logSink {
	return &logSink{want: want, ready: make(chan struct{})}
}

func (s *logSink) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, err := s.buf.Write(p)
	if strings.Contains(s.buf.String(), s.want) {
		s.once.Do(func() { close(s.ready) })
	}
	return n, err
}

func (s *logSink) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.String()
}

// startSSEWithDebugLog は Debug レベルの logger を注入した client で SSE を開始し、
// sink に want が現れるまで待つ。現れなければ t.Fatal。
func startSSEWithDebugLog(t *testing.T, srv *httptest.Server, want string) *logSink {
	t.Helper()
	sink := newLogSink(want)
	logger := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelDebug}))
	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "", agenthub.WithLogger(logger))
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

	select {
	case <-sink.ready:
	case <-time.After(5 * time.Second):
		t.Fatalf("timeout: no log line containing %q; got:\n%s", want, sink.String())
	}
	return sink
}

// 壊れた JSON を無言で捨てず、切り分け用の Debug ログを 1 行残すこと (issue #79)。
// 修正前は handleSSEEvent が unmarshal エラーで無言 return していた。
func TestSSEStream_logsUnparsableEvent(t *testing.T) {
	const broken = `{"jsonrpc":"2.0","method":`
	srv := sseStreamServer(t, func(w http.ResponseWriter) {
		writeSSEHeaders(w)
		fmt.Fprintf(w, "event: message\ndata: %s\n\n", broken)
	})

	got := startSSEWithDebugLog(t, srv, "not valid JSON").String()
	if !strings.Contains(got, "level=DEBUG") {
		t.Errorf("want the drop reported at DEBUG level, got:\n%s", got)
	}
	// payload は rawSnippet の %q と slog TextHandler の quoting で二重に escape される
	// ため生文字列では一致しない。全長と、escape を挟んでも残る識別可能な断片で見る。
	if !strings.Contains(got, fmt.Sprintf("raw %d bytes", len(broken))) {
		t.Errorf("want the payload length reported, got:\n%s", got)
	}
	if !strings.Contains(got, "jsonrpc") {
		t.Errorf("log should carry the offending payload, got:\n%s", got)
	}
	// unmarshal エラー自体も落とさないこと
	if !strings.Contains(got, "unexpected end of JSON input") {
		t.Errorf("want the unmarshal error preserved, got:\n%s", got)
	}
}

// 壊れた payload が巨大でもログは rawSnippet の方針 (全長 + 先頭 N バイト) で
// 切り詰められ、全長は数値で残ること。N は errRawSnippetBytes 依存なので literal で
// 比較しない (issue #77 と同じ方針)。
func TestSSEStream_unparsableEventLogIsTruncated(t *testing.T) {
	broken := `{"jsonrpc":"2.0","method":` + strings.Repeat("x", 8192)
	srv := sseStreamServer(t, func(w http.ResponseWriter) {
		writeSSEHeaders(w)
		fmt.Fprintf(w, "event: message\ndata: %s\n\n", broken)
	})

	got := startSSEWithDebugLog(t, srv, "not valid JSON").String()

	m := regexp.MustCompile(`raw (\d+) bytes, first (\d+):`).FindStringSubmatch(got)
	if m == nil {
		t.Fatalf("want a truncation marker of the form %q, got:\n%s", "raw N bytes, first M:", got)
	}
	full, _ := strconv.Atoi(m[1])
	if full != len(broken) {
		t.Errorf("want the full payload length %d reported, got %d", len(broken), full)
	}
	limit, _ := strconv.Atoi(m[2])
	if limit >= full {
		t.Errorf("payload was not truncated: first %d of %d bytes", limit, full)
	}
	// 切り詰めたのに全文が載っていたら意味がない
	if strings.Contains(got, strings.Repeat("x", 8192)) {
		t.Errorf("the full payload leaked into the log despite the truncation marker")
	}
}

// ──────────────────────────────────────────────────────────────────────── //
// SSE 周辺の並行経路と dispatch 回数 (issue #80)                            //
// ──────────────────────────────────────────────────────────────────────── //

// lockedBuffer は SSE goroutine と test goroutine から並行に触れる log 出力先。
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// SSE ストリームと RPC poll の両方から warnOversizedLine を叩いても、WARN が 1 回に収まること。
// 行サイズはどちらも 2 MiB 前後で、互いに 2 倍未満なのでレート制限上は 1 回。
// 実際の呼び出しはほぼ直列で CAS の衝突経路を通らないため、sseWarnedBytes の atomic 退行は
// このテストでは検出できない — それは TestOversizedWarn_concurrentCallersContendOnCAS が守る (issue #94)。
func TestOversizedWarn_concurrentSSEAndPolls(t *testing.T) {
	const (
		lineBytes = 2 << 20 // 閾値 1 MiB 超え
		sseEvents = 4
		polls     = 8
	)
	padded := fmt.Sprintf(`{"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":"inbox://@bridge-test","pad":"%s"}}`, strings.Repeat("x", lineBytes))

	var reqCount atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("mcp-session-id", "test-session-id")
		if r.Method == http.MethodGet {
			writeSSEHeaders(w)
			for i := 0; i < sseEvents; i++ {
				fmt.Fprintf(w, "event: message\ndata: %s\n\n", padded)
				if f, ok := w.(http.Flusher); ok {
					f.Flush()
				}
			}
			<-r.Context().Done() // 再接続させない
			return
		}
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
			payload, _ := json.Marshal([]map[string]string{{
				"id": "big1", "from": "@alice", "to": "@bridge-test",
				"message": strings.Repeat("x", lineBytes),
			}})
			inner, _ := json.Marshal(string(payload))
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":%d,\"result\":{\"content\":[{\"type\":\"text\",\"text\":%s}]}}\n\n", n, inner)
		}
	}))
	t.Cleanup(srv.Close)

	var logBuf lockedBuffer
	logger := slog.New(slog.NewTextHandler(&logBuf, &slog.HandlerOptions{Level: slog.LevelWarn}))
	c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "", agenthub.WithLogger(logger))
	if err != nil {
		t.Fatal(err)
	}
	var pushes atomic.Int32
	allPushed := make(chan struct{})
	c.OnInboxPush(func() {
		if pushes.Add(1) == sseEvents {
			close(allPushed)
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

	// poll は 1 goroutine から連打する (SSE goroutine と並行)。
	// 複数 goroutine からの並行 RPC は sessionID 書き込みが別途競合するため対象外。
	for i := 0; i < polls; i++ {
		if _, err := c.GetMessages(ctx); err != nil {
			t.Fatalf("GetMessages #%d: %v", i, err)
		}
	}

	select {
	case <-allPushed:
	case <-time.After(10 * time.Second):
		t.Fatalf("timeout: only %d/%d oversized SSE events dispatched", pushes.Load(), sseEvents)
	}

	if got := countOversizedWarns(logBuf.String()); got != 1 {
		t.Errorf("want exactly 1 WARN across concurrent SSE + polls, got %d:\n%s", got, logBuf.String())
	}
}

// 複数 goroutine が同時に warnOversizedLine を呼んでも、データ競合なく (-race) WARN が 1 回に収まること。
// 開始チャネルで一斉に放し、「全員が 0 を Load し、CAS に負けた側が再試行する」経路を通す。
// sseWarnedBytes を atomic でない int64 に戻すと -race で DATA RACE になる (issue #94)。
func TestOversizedWarn_concurrentCallersContendOnCAS(t *testing.T) {
	const (
		lineBytes = 2 << 20 // 閾値 1 MiB 超え
		callers   = 8
	)
	var logBuf lockedBuffer
	logger := slog.New(slog.NewTextHandler(&logBuf, &slog.HandlerOptions{Level: slog.LevelWarn}))
	c, err := agenthub.New("http://127.0.0.1:0", "ghp_test", "bridge-test", "", agenthub.WithLogger(logger))
	if err != nil {
		t.Fatal(err)
	}

	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			c.WarnOversizedLineForTest(lineBytes)
		}()
	}
	close(start)
	wg.Wait()

	if got := countOversizedWarns(logBuf.String()); got != 1 {
		t.Errorf("want exactly 1 WARN across %d concurrent callers, got %d:\n%s", callers, got, logBuf.String())
	}
}

// 1 つのイベントは、終端の形によらずちょうど 1 回だけ dispatch されること。
// startSSEWithCallback の latch は 1 回目で閉じるため、二重 dispatch を見逃す。
// ここでは回数を数え、ストリーム終了後の再接続 (GET 2 本目) を「1 本目の処理が
// 全て終わった」合図にしてから判定する。
func TestSSEStream_eventDispatchedExactlyOnce(t *testing.T) {
	cases := []struct {
		name   string
		stream string
	}{
		{"blank line then EOF", "event: message\ndata: " + inboxNotification + "\n\n"},
		{"newline then EOF", "event: message\ndata: " + inboxNotification + "\n"},
		{"mid-line EOF", "event: message\ndata: " + inboxNotification},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var gets atomic.Int32
			reconnected := make(chan struct{})
			var reconnectOnce sync.Once
			srv := sseStreamServer(t, func(w http.ResponseWriter) {
				writeSSEHeaders(w)
				if gets.Add(1) == 1 {
					fmt.Fprint(w, tc.stream)
					return
				}
				// StopSSE までに 3 本目以降の GET が来ても二重 close しない
				reconnectOnce.Do(func() { close(reconnected) })
			})

			c, err := agenthub.New(srv.URL, "ghp_test", "bridge-test", "")
			if err != nil {
				t.Fatal(err)
			}
			var calls atomic.Int32
			c.OnInboxPush(func() { calls.Add(1) })
			ctx := t.Context()
			if err := c.Initialize(ctx); err != nil {
				t.Fatalf("Initialize: %v", err)
			}
			if err := c.StartSSE(ctx); err != nil {
				t.Fatalf("StartSSE: %v", err)
			}

			select {
			case <-reconnected:
			case <-time.After(10 * time.Second):
				t.Fatal("timeout: SSE did not reconnect after the first stream ended")
			}
			c.StopSSE()
			// callback は go fn() で起動されるので、実行が追いつくのを少し待つ。
			time.Sleep(100 * time.Millisecond)

			if got := calls.Load(); got != 1 {
				t.Errorf("want exactly 1 dispatch, got %d", got)
			}
		})
	}
}
