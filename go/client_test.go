package agenthub_test

import (
	"strings"
	"testing"

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
