package agenthub_test

import (
	"testing"

	agenthub "github.com/kishibashi3/agent-hub-sdk/go"
)

func TestParseMessages_empty(t *testing.T) {
	msgs, err := agenthub.ParseMessages("")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if msgs != nil {
		t.Fatalf("want nil, got %v", msgs)
	}
}

func TestParseMessages_emptyArray(t *testing.T) {
	msgs, err := agenthub.ParseMessages("[]")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(msgs) != 0 {
		t.Fatalf("want 0, got %d", len(msgs))
	}
}

func TestParseMessages_valid(t *testing.T) {
	input := `[
		{"id":"abc","from":"@alice","to":"@bob","message":"hello","caused_by":"","timestamp":"2026-01-01T00:00:00Z"},
		{"id":"def","from":"@carol","to":"@bob","message":"world","timestamp":"2026-01-01T00:00:01Z"}
	]`
	msgs, err := agenthub.ParseMessages(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(msgs) != 2 {
		t.Fatalf("want 2 messages, got %d", len(msgs))
	}
	if msgs[0].ID != "abc" || msgs[0].Sender != "@alice" || msgs[0].Body != "hello" {
		t.Errorf("unexpected msg[0]: %+v", msgs[0])
	}
	if msgs[1].ID != "def" || msgs[1].Sender != "@carol" {
		t.Errorf("unexpected msg[1]: %+v", msgs[1])
	}
}

func TestParseMessages_skipMissingFields(t *testing.T) {
	// id か sender (from) が空のエントリは silent skip する。
	// 外側の配列は valid JSON である必要がある。
	input := `[
		{"id":"ok","from":"@alice","to":"@bob","message":"good"},
		{"id":"","from":"@bob","to":"@alice","message":"no id"},
		{"id":"nofrom","from":"","to":"@alice","message":"no sender"},
		{"id":"extra-fields","from":"@x","to":"@y","message":"ok","unknown_field":"ignored"}
	]`
	msgs, err := agenthub.ParseMessages(input)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// "ok" と "extra-fields" の 2 件が valid; 空 id / 空 sender は除外
	if len(msgs) != 2 {
		t.Fatalf("want 2 valid messages, got %d: %+v", len(msgs), msgs)
	}
	if msgs[0].ID != "ok" {
		t.Errorf("unexpected msg[0]: %+v", msgs[0])
	}
	if msgs[1].ID != "extra-fields" {
		t.Errorf("unexpected msg[1]: %+v", msgs[1])
	}
}

func TestParseMessages_invalidJSON(t *testing.T) {
	_, err := agenthub.ParseMessages("not-json")
	if err == nil {
		t.Fatal("want error for invalid JSON, got nil")
	}
}
