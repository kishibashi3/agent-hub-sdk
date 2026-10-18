// Package agenthub provides a Go SDK for agent-hub.
//
// Typical usage:
//
//	client := agenthub.New(endpoint, pat, userID, tenantID)
//	if err := client.Initialize(ctx); err != nil { ... }
//	if _, err := client.Register(ctx, "My Bridge", "stateful"); err != nil { ... }
//
//	msgs, err := client.GetMessages(ctx)
//	for _, msg := range msgs {
//	    // handle msg
//	    client.MarkAsRead(ctx, msg.ID)
//	}
package agenthub

import (
	"encoding/json"
	"fmt"
)

// Message は agent-hub の get_messages が返す 1 件のメッセージ。
// wire format: {id, from, to, message, caused_by, timestamp}
type Message struct {
	ID        string `json:"id"`
	Sender    string `json:"from"`     // wire: "from"
	To        string `json:"to"`
	Body      string `json:"message"`  // wire: "message"
	CausedBy  string `json:"caused_by"`
	Timestamp string `json:"timestamp"`
}

// ParseMessages は get_messages ツールのテキスト結果 (JSON 配列) をパースする。
// 壊れた行は silent skip する (defensive parse)。
// text が空文字列の場合は nil スライスを返す (エラーなし)。
func ParseMessages(text string) ([]Message, error) {
	if text == "" {
		return nil, nil
	}
	var raw []json.RawMessage
	if err := json.Unmarshal([]byte(text), &raw); err != nil {
		return nil, fmt.Errorf("get_messages parse: %w", err)
	}
	msgs := make([]Message, 0, len(raw))
	for _, r := range raw {
		var m Message
		if err := json.Unmarshal(r, &m); err != nil {
			continue // skip malformed entry
		}
		if m.ID == "" || m.Sender == "" {
			continue
		}
		msgs = append(msgs, m)
	}
	return msgs, nil
}

// Thread は agent-hub の get_thread が返すスレッド (caused_by 鎖) 全体。
// wire format: {root_id, thread_size, messages[]}
// Messages は時系列順 (古い順)。root の CausedBy は空文字列。
type Thread struct {
	RootID     string    `json:"root_id"`
	ThreadSize int       `json:"thread_size"`
	Messages   []Message `json:"messages"`
}

// ParseThread は get_thread ツールのテキスト結果 (JSON object) をパースする。
//
// server は閲覧権限なし・存在しない ID 等の失敗を isError ではなく
// {"error": "..."} のテキストで返すため、error フィールドがあれば error を返す。
// 空配列の成功と取り違えないよう、root_id が無い応答も error とする。
func ParseThread(text string) (*Thread, error) {
	var raw struct {
		Thread
		Error string `json:"error"`
	}
	if err := json.Unmarshal([]byte(text), &raw); err != nil {
		return nil, fmt.Errorf("get_thread parse: %w", err)
	}
	if raw.Error != "" {
		return nil, fmt.Errorf("server error: %s", raw.Error)
	}
	if raw.RootID == "" {
		return nil, fmt.Errorf("get_thread parse: root_id missing (%s)", rawSnippet([]byte(text)))
	}
	th := raw.Thread
	return &th, nil
}
