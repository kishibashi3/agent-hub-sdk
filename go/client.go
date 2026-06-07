package agenthub

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	mcpSessionIDHeader = "mcp-session-id"
	jsonContentType    = "application/json"
	sseContentType     = "text/event-stream"
	mcpProtocolVersion = "2024-11-05"

	defaultClientName    = "agent-hub-sdk-go"
	defaultClientVersion = "0.1.0"
)

// Client は agent-hub MCP エンドポイントとの接続を管理する。
//
// 実装している MCP 操作:
//   - initialize + notifications/initialized (セッション確立)
//   - tools/call: register / get_messages / mark_as_read / send_message
//   - GET /mcp SSE ストリーム: サーバー ping に自動応答 (issue #41)
//
// SSE 対応:
//   - tools/call の応答は JSON または text/event-stream のどちらも受け取る。
//   - StartSSE() で GET /mcp の long-lived connection を開き ping に応答する。
//
// # Concurrency
//
// postRPC / tools/call は単一 goroutine からの順次呼び出しを前提とする。
// sessionID フィールドは sync なしで読み書きされるため、複数 goroutine から
// 並列に呼び出すと data race になる。bridge は 1 メッセージを逐次処理する
// 設計なので問題ないが、並列化が必要な場合は呼び出し側で sync.Mutex を使うこと。
//
// SSE goroutine (StartSSE が起動する) は sessionID を初期化時のスナップショットで
// 保持するため、main goroutine の sessionID アクセスと競合しない。
// sendPong は sseClient (Timeout=0 専用クライアント) を使い、
// main goroutine の httpClient 呼び出しと独立して並列動作する。
type Client struct {
	endpoint   string
	pat        string
	userID     string
	tenantID   string
	clientName string
	sessionID  string   // single-goroutine 前提; 並列アクセス不可
	reqIDSeq   atomic.Int64
	httpClient *http.Client
	// sseClient は SSE ストリーム (GET /mcp) 専用クライアント (Timeout=0 = long-lived)。
	sseClient *http.Client
	sseMu     sync.Mutex // sseCancel / sseDone を保護する
	sseCancel context.CancelFunc
	sseDone   <-chan struct{}
}

// ClientOption は Client の追加設定を行うオプション関数。
type ClientOption func(*Client)

// WithClientName は MCP initialize ハンドシェイクで送る clientInfo.name を上書きする。
// デフォルト: "agent-hub-sdk-go"。
func WithClientName(name string) ClientOption {
	return func(c *Client) { c.clientName = name }
}

// WithHTTPTimeout は HTTP クライアントのタイムアウトを上書きする。
// デフォルト: 90 秒。
func WithHTTPTimeout(d time.Duration) ClientOption {
	return func(c *Client) { c.httpClient.Timeout = d }
}

// WithTransport は httpClient / sseClient が使う http.Transport を差し替える。
// 主にテストや CI 環境でのカスタム transport 注入用。
// sseClient は Timeout=0 (long-lived) で同じ transport を共有する。
func WithTransport(t *http.Transport) ClientOption {
	return func(c *Client) {
		c.httpClient.Transport = t
		c.sseClient.Transport = t
	}
}

// newTransport は TCP keepalive を明示した per-client http.Transport を生成する。
// http.DefaultTransport を共有しないことで接続プールを分離し、
// ルーターに古い pooled connection を刈り取られる問題を防ぐ (issue #185)。
//
// KeepAlive: 30s — OS 側 TCP keepalive probe を 30 秒ごとに送信し NAT セッションを維持する。
// IdleConnTimeout: 55s — ルーターの NAT idle timeout (一般的に 60〜300s) より短く設定し、
// pool 内の dead connection を先回りして破棄する。
func newTransport() *http.Transport {
	dialer := &net.Dialer{
		Timeout:   30 * time.Second,
		KeepAlive: 30 * time.Second,
	}
	return &http.Transport{
		DialContext:         dialer.DialContext,
		IdleConnTimeout:     55 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
		MaxIdleConns:        10,
		MaxIdleConnsPerHost: 2,
	}
}

// New は新しい Client を生成する。Initialize() を呼ぶまで tools/call はできない。
//
// endpoint・pat・userID は必須パラメータ。空文字列を渡すとエラーを返す (fail-fast)。
// tenantID は省略可能 (空文字列 = default tenant)。
func New(endpoint, pat, userID, tenantID string, opts ...ClientOption) (*Client, error) {
	if endpoint == "" {
		return nil, fmt.Errorf("agenthub.New: endpoint is required")
	}
	if pat == "" {
		return nil, fmt.Errorf("agenthub.New: pat is required")
	}
	if userID == "" {
		return nil, fmt.Errorf("agenthub.New: userID is required")
	}
	t := newTransport()
	c := &Client{
		endpoint:   endpoint,
		pat:        pat,
		userID:     userID,
		tenantID:   tenantID,
		clientName: defaultClientName,
		httpClient: &http.Client{Timeout: 90 * time.Second, Transport: t},
		sseClient:  &http.Client{Timeout: 0, Transport: t}, // long-lived SSE 接続 — タイムアウト無効
	}
	for _, o := range opts {
		o(c)
	}
	return c, nil
}

// ──────────────────────────────────────────────────────────────────────── //
// セッション確立                                                           //
// ──────────────────────────────────────────────────────────────────────── //

// Initialize は MCP initialize ハンドシェイクを行い、sessionID を確立する。
// initialize → notifications/initialized の順で送信する (MCP 仕様)。
// 再接続時に古い sessionID が残っていると HTTP 400 になるため、先頭でクリアする。
func (c *Client) Initialize(ctx context.Context) error {
	c.sessionID = "" // stale session ID をクリア (issue #41)
	params := map[string]any{
		"protocolVersion": mcpProtocolVersion,
		"capabilities":    map[string]any{},
		"clientInfo": map[string]any{
			"name":    c.clientName,
			"version": defaultClientVersion,
		},
	}
	if _, err := c.postRPC(ctx, "initialize", params, false); err != nil {
		return fmt.Errorf("initialize: %w", err)
	}
	if err := c.postNotification(ctx, "notifications/initialized", nil); err != nil {
		return fmt.Errorf("notifications/initialized: %w", err)
	}
	return nil
}

// ──────────────────────────────────────────────────────────────────────── //
// tools/call ラッパー                                                      //
// ──────────────────────────────────────────────────────────────────────── //

// Register は自 peer を agent-hub に登録する。
// displayName と mode は空文字列を渡すと省略される。
func (c *Client) Register(ctx context.Context, displayName, mode string) (string, error) {
	args := map[string]any{"name": c.userID}
	if displayName != "" {
		args["display_name"] = displayName
	}
	if mode != "" {
		args["mode"] = mode
	}
	text, err := c.callToolText(ctx, "register", args)
	if err != nil {
		return "", fmt.Errorf("register: %w", err)
	}
	return text, nil
}

// GetMessages は未読メッセージ一覧を取得する。
// 未読がない場合は空スライスを返す (エラーなし)。
func (c *Client) GetMessages(ctx context.Context) ([]Message, error) {
	text, err := c.callToolText(ctx, "get_messages", nil)
	if err != nil {
		return nil, fmt.Errorf("get_messages: %w", err)
	}
	return ParseMessages(text)
}

// MarkAsRead は指定 ID のメッセージを既読にする (= ack)。
func (c *Client) MarkAsRead(ctx context.Context, msgID string) error {
	args := map[string]any{"message_id": msgID}
	if _, err := c.callToolText(ctx, "mark_as_read", args); err != nil {
		return fmt.Errorf("mark_as_read %s: %w", msgID, err)
	}
	return nil
}

// SendMessage は指定の宛先に DM を送る。
// causedBy が空文字列の場合は省略される (因果チェーン任意)。
func (c *Client) SendMessage(ctx context.Context, to, body, causedBy string) error {
	args := map[string]any{
		"to":      to,
		"message": body,
	}
	if causedBy != "" {
		args["caused_by"] = causedBy
	}
	if _, err := c.callToolText(ctx, "send_message", args); err != nil {
		return fmt.Errorf("send_message to %s: %w", to, err)
	}
	return nil
}

// ──────────────────────────────────────────────────────────────────────── //
// 内部実装                                                                 //
// ──────────────────────────────────────────────────────────────────────── //

type rpcRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      *int64 `json:"id,omitempty"` // 通知は omit
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      *int64          `json:"id"`
	Result  json.RawMessage `json:"result"`
	Error   *rpcError       `json:"error"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type toolCallParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments,omitempty"`
}

type toolResult struct {
	Content []contentBlock `json:"content"`
	IsError bool           `json:"isError"`
}

type contentBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

func (c *Client) callToolText(ctx context.Context, name string, args map[string]any) (string, error) {
	params := toolCallParams{Name: name, Arguments: args}
	data, err := c.postRPC(ctx, "tools/call", params, false)
	if err != nil {
		return "", err
	}
	var rpc rpcResponse
	if err := json.Unmarshal(data, &rpc); err != nil {
		return "", fmt.Errorf("unmarshal rpc response: %w (raw: %q)", err, string(data))
	}
	if rpc.Error != nil {
		return "", fmt.Errorf("rpc error %d: %s", rpc.Error.Code, rpc.Error.Message)
	}
	if len(rpc.Result) == 0 {
		return "", fmt.Errorf("rpc result is null for tool %q (no result field in response)", name)
	}
	var result toolResult
	if err := json.Unmarshal(rpc.Result, &result); err != nil {
		return "", fmt.Errorf("unmarshal tool result: %w", err)
	}
	if result.IsError {
		return "", fmt.Errorf("tool returned isError: %s", joinText(result.Content))
	}
	return joinText(result.Content), nil
}

func (c *Client) postNotification(ctx context.Context, method string, params any) error {
	_, err := c.postRPC(ctx, method, params, true)
	return err
}

// postRPC は JSON-RPC リクエストを POST して生のレスポンスボディを返す。
// isNotification=true の場合は id を付けず、レスポンスボディは破棄する。
func (c *Client) postRPC(ctx context.Context, method string, params any, isNotification bool) ([]byte, error) {
	var id *int64
	if !isNotification {
		n := c.reqIDSeq.Add(1)
		id = &n
	}

	reqBody, err := json.Marshal(rpcRequest{
		JSONRPC: "2.0",
		ID:      id,
		Method:  method,
		Params:  params,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(reqBody))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", jsonContentType)
	req.Header.Set("Accept", jsonContentType+", "+sseContentType)
	req.Header.Set("Authorization", "Bearer "+c.pat)
	req.Header.Set("X-User-Id", c.userID)
	if c.tenantID != "" {
		req.Header.Set("X-Tenant-Id", c.tenantID)
	}
	if c.sessionID != "" {
		req.Header.Set(mcpSessionIDHeader, c.sessionID)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http do: %w", err)
	}
	defer resp.Body.Close()

	if sid := resp.Header.Get(mcpSessionIDHeader); sid != "" {
		c.sessionID = sid
	}

	if isNotification {
		io.Copy(io.Discard, resp.Body) //nolint:errcheck
		return nil, nil
	}

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	ct := resp.Header.Get("Content-Type")
	if strings.HasPrefix(ct, sseContentType) {
		return readFirstSSEData(resp.Body)
	}
	return io.ReadAll(resp.Body)
}

// ──────────────────────────────────────────────────────────────────────── //
// SSE ストリーム (GET /mcp) — ping 自動応答                               //
// ──────────────────────────────────────────────────────────────────────── //

// pingMessage は SSE ストリーム経由で受信する JSON-RPC ping リクエスト。
// ID は int または string のどちらでもよく、pong でそのままエコーバックする。
type pingMessage struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
}

// StartSSE は GET /mcp SSE ストリームを開き、ping に自動応答する
// バックグラウンド goroutine を起動する (issue #41)。
//
// Initialize() を呼び出した後に呼ぶこと (sessionID が設定されている必要がある)。
// 既に SSE goroutine が起動中の場合は StopSSE() を先に呼ぶこと。
// ctx がキャンセルされると goroutine も終了する (StopSSE でも停止可)。
func (c *Client) StartSSE(ctx context.Context) error {
	sid := c.sessionID
	if sid == "" {
		return fmt.Errorf("StartSSE: sessionID is empty; call Initialize() first")
	}

	sseCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})

	c.sseMu.Lock()
	c.sseCancel = cancel
	c.sseDone = done
	c.sseMu.Unlock()

	go c.handleSSEStream(sseCtx, sid, done)
	return nil
}

// StopSSE は SSE goroutine をキャンセルして終了を待つ。
// StartSSE が呼ばれていない場合は no-op。
func (c *Client) StopSSE() {
	c.sseMu.Lock()
	cancel := c.sseCancel
	done := c.sseDone
	c.sseCancel = nil
	c.sseDone = nil
	c.sseMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}
}

// handleSSEStream は SSE 接続を管理し、切断時に再接続するバックグラウンドループ。
// ctx がキャンセルされると即座に終了する。
func (c *Client) handleSSEStream(ctx context.Context, sid string, done chan struct{}) {
	defer close(done)
	for {
		if ctx.Err() != nil {
			return
		}
		if err := c.runSSELoop(ctx, sid); err != nil {
			if ctx.Err() != nil {
				return
			}
			// 再接続前に短時間待機する
			select {
			case <-ctx.Done():
				return
			case <-time.After(3 * time.Second):
			}
		}
	}
}

// runSSELoop は GET /mcp SSE ストリームを読み続け、ping イベントに応答する。
// 接続が切断されるか ctx がキャンセルされると返る。
func (c *Client) runSSELoop(ctx context.Context, sid string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.endpoint, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", sseContentType)
	req.Header.Set("Authorization", "Bearer "+c.pat)
	req.Header.Set("X-User-Id", c.userID)
	if c.tenantID != "" {
		req.Header.Set("X-Tenant-Id", c.tenantID)
	}
	req.Header.Set(mcpSessionIDHeader, sid)

	resp, err := c.sseClient.Do(req)
	if err != nil {
		return fmt.Errorf("SSE GET: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("SSE GET HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 128*1024), 128*1024)

	var dataLines []string
	for scanner.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		line := scanner.Text()
		if line == "" {
			if len(dataLines) > 0 {
				c.handleSSEEvent(ctx, sid, []byte(strings.Join(dataLines, "\n")))
				dataLines = dataLines[:0]
			}
		} else if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		}
	}
	if err := scanner.Err(); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("SSE scan: %w", err)
	}
	return io.EOF
}

// handleSSEEvent は 1 つの SSE データブロックを処理する。
// ping リクエストを受け取った場合は pong を返す。
func (c *Client) handleSSEEvent(ctx context.Context, sid string, data []byte) {
	var msg pingMessage
	if err := json.Unmarshal(data, &msg); err != nil {
		return
	}
	if msg.Method == "ping" {
		c.sendPong(ctx, sid, msg.ID)
	}
}

// sendPong は ping に対して pong (JSON-RPC result: {}) を POST で返す。
// sessionID はパラメータ sid で受け取り c.sessionID を読まない (data race 回避)。
// 8 秒タイムアウトを設定して PING_TIMEOUT_MS (10 秒) 内に応答できるようにする。
func (c *Client) sendPong(ctx context.Context, sid string, id json.RawMessage) {
	pongCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()

	type pongPayload struct {
		JSONRPC string          `json:"jsonrpc"`
		ID      json.RawMessage `json:"id"`
		Result  json.RawMessage `json:"result"`
	}
	body, err := json.Marshal(pongPayload{
		JSONRPC: "2.0",
		ID:      id,
		Result:  json.RawMessage(`{}`),
	})
	if err != nil {
		return
	}

	req, err := http.NewRequestWithContext(pongCtx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", jsonContentType)
	req.Header.Set("Accept", jsonContentType+", "+sseContentType)
	req.Header.Set("Authorization", "Bearer "+c.pat)
	req.Header.Set("X-User-Id", c.userID)
	if c.tenantID != "" {
		req.Header.Set("X-Tenant-Id", c.tenantID)
	}
	req.Header.Set(mcpSessionIDHeader, sid)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body) //nolint:errcheck
}

// ──────────────────────────────────────────────────────────────────────── //

// readFirstSSEData は SSE ストリームから最初のイベントの data を返す。
// agent-hub の tools/call は 1 件のレスポンスしか送らないのでこれで十分。
func readFirstSSEData(r io.Reader) ([]byte, error) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 128*1024), 128*1024)

	var dataLines []string
	inEvent := false

	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case line == "":
			if inEvent && len(dataLines) > 0 {
				return []byte(strings.Join(dataLines, "\n")), nil
			}
			dataLines = dataLines[:0]
			inEvent = false
		case strings.HasPrefix(line, "event:"):
			inEvent = true
		case strings.HasPrefix(line, "data:"):
			inEvent = true
			dataLines = append(dataLines, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("SSE scan: %w", err)
	}
	if len(dataLines) > 0 {
		return []byte(strings.Join(dataLines, "\n")), nil
	}
	return nil, io.EOF
}

func joinText(blocks []contentBlock) string {
	var parts []string
	for _, b := range blocks {
		if b.Type == "text" && b.Text != "" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "\n")
}
