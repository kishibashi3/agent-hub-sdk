package agenthub

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
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
//
// SSE 対応: tools/call の応答は JSON または text/event-stream のどちらも受け取る。
// SSE inbox push は未実装 — ポーリング (GetMessages) で代替する (MVP)。
type Client struct {
	endpoint   string
	pat        string
	userID     string
	tenantID   string
	clientName string
	sessionID  string
	reqIDSeq   atomic.Int64
	httpClient *http.Client
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

// New は新しい Client を生成する。Initialize() を呼ぶまで tools/call はできない。
func New(endpoint, pat, userID, tenantID string, opts ...ClientOption) *Client {
	c := &Client{
		endpoint:   endpoint,
		pat:        pat,
		userID:     userID,
		tenantID:   tenantID,
		clientName: defaultClientName,
		httpClient: &http.Client{Timeout: 90 * time.Second},
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// ──────────────────────────────────────────────────────────────────────── //
// セッション確立                                                           //
// ──────────────────────────────────────────────────────────────────────── //

// Initialize は MCP initialize ハンドシェイクを行い、sessionID を確立する。
// initialize → notifications/initialized の順で送信する (MCP 仕様)。
func (c *Client) Initialize(ctx context.Context) error {
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
