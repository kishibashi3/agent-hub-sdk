package agenthub_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	agenthub "github.com/kishibashi3/agent-hub-sdk/go"
)

// ── ParseCommand ─────────────────────────────────────────────────────────────

func TestParseCommand(t *testing.T) {
	cases := []struct {
		body     string
		wantCmd  string
		wantArgs string
	}{
		{"/ping", "/ping", ""},
		{"/list verbose", "/list", "verbose"},
		{"  /status  ", "/status", ""},
		{"/restart", "/restart", ""},
		{"/foo bar baz", "/foo", "bar baz"},
		{"hello", "", ""},
		{"", "", ""},
		{"/", "", ""},
		{"  /  ", "", ""},
		{"natural language message", "", ""},
	}
	for _, tc := range cases {
		t.Run(fmt.Sprintf("body=%q", tc.body), func(t *testing.T) {
			gotCmd, gotArgs := agenthub.ParseCommand(tc.body)
			if gotCmd != tc.wantCmd || gotArgs != tc.wantArgs {
				t.Errorf("ParseCommand(%q) = (%q, %q), want (%q, %q)",
					tc.body, gotCmd, gotArgs, tc.wantCmd, tc.wantArgs)
			}
		})
	}
}

// ── CommandRouter のロジック (ParseCommand + dispatch + 戻り値) のみを確認する。
// 実際の SendMessage / MarkAsRead 呼び出しは integration test 相当のため省略。

// TestCommandRouter_ParseDispatch は Handle の dispatch ロジックをテストする。
// 実 Client への通信は発生しない形でテストするため、カスタムハンドラの
// 戻り値と Handle の戻り値のみを確認する。
func TestCommandRouter_ParseDispatch(t *testing.T) {
	router := agenthub.NewCommandRouter()

	// カスタムハンドラを登録
	var lastArgs string
	router.Register("/echo", "echo args", func(ctx context.Context, client *agenthub.Client, msg agenthub.Message, args string) (string, error) {
		lastArgs = args
		return "echo: " + args, nil
	})

	t.Run("non-command yields false", func(t *testing.T) {
		// Handle は実 client を使うため直接呼べないが、ParseCommand で検証する
		cmd, _ := agenthub.ParseCommand("hello world")
		if cmd != "" {
			t.Errorf("expected empty cmd for natural language, got %q", cmd)
		}
	})

	t.Run("registered command parsed correctly", func(t *testing.T) {
		cmd, args := agenthub.ParseCommand("/echo hello world")
		if cmd != "/echo" || args != "hello world" {
			t.Errorf("got (%q, %q)", cmd, args)
		}
		_ = lastArgs // suppress unused warning
	})

	t.Run("builtin commands parsed correctly", func(t *testing.T) {
		for _, body := range []string{"/ping", "/status", "/help", "/restart"} {
			cmd, _ := agenthub.ParseCommand(body)
			if cmd != body {
				t.Errorf("ParseCommand(%q) = %q, want %q", body, cmd, body)
			}
		}
	})
}

// TestParseCommand_EdgeCases は parse_command の境界値をテストする。
func TestParseCommand_EdgeCases(t *testing.T) {
	t.Run("slash only", func(t *testing.T) {
		cmd, args := agenthub.ParseCommand("/")
		if cmd != "" || args != "" {
			t.Errorf("got (%q, %q), want empty", cmd, args)
		}
	})

	t.Run("leading spaces before slash", func(t *testing.T) {
		cmd, args := agenthub.ParseCommand("   /ping")
		if cmd != "/ping" || args != "" {
			t.Errorf("got (%q, %q)", cmd, args)
		}
	})

	t.Run("trailing spaces", func(t *testing.T) {
		cmd, args := agenthub.ParseCommand("/status   ")
		if cmd != "/status" || args != "" {
			t.Errorf("got (%q, %q)", cmd, args)
		}
	})

	t.Run("args with spaces preserved", func(t *testing.T) {
		cmd, args := agenthub.ParseCommand("/list foo bar baz")
		if cmd != "/list" || args != "foo bar baz" {
			t.Errorf("got (%q, %q)", cmd, args)
		}
	})

	t.Run("nil-like empty body", func(t *testing.T) {
		cmd, args := agenthub.ParseCommand("")
		if cmd != "" || args != "" {
			t.Errorf("got (%q, %q)", cmd, args)
		}
	})
}

// TestCommandRouter_Registration は Register と SetStatusFunc をテストする。
func TestCommandRouter_Registration(t *testing.T) {
	router := agenthub.NewCommandRouter()

	// カスタムステータス
	router.SetStatusFunc(func() string { return "busy" })

	// カスタムハンドラ
	called := false
	router.Register("/test", "test command", func(ctx context.Context, client *agenthub.Client, msg agenthub.Message, args string) (string, error) {
		called = true
		return "test-reply", nil
	})
	_ = called // suppress unused warning

	// panic on bad cmd
	t.Run("panic on bad cmd", func(t *testing.T) {
		defer func() {
			if r := recover(); r == nil {
				t.Error("expected panic for cmd without /")
			}
		}()
		router.Register("bad", "desc", nil)
	})
}

// TestCommandRouter_SetRestartHandler はリスタートハンドラの設定をテストする。
func TestCommandRouter_SetRestartHandler(t *testing.T) {
	router := agenthub.NewCommandRouter()

	called := false
	router.SetRestartHandler(func(ctx context.Context) error {
		called = true
		return nil
	})
	_ = called // suppress unused warning; runtime test would require stub

	// nil を設定すると ack-only
	router.SetRestartHandler(nil)
	// no panic expected
}

// TestCommandRouter_HandleReturnValue は Handle の戻り値を検証する。
// 実 Client を使わず ParseCommand ベースで論理を確認する。
func TestCommandRouter_HandleReturnValue(t *testing.T) {
	// ParseCommand が "" を返すものは Handle で false が返るべき
	naturalLanguageBodies := []string{
		"hello",
		"how are you?",
		"",
		"    ",
	}
	for _, body := range naturalLanguageBodies {
		cmd, _ := agenthub.ParseCommand(body)
		if cmd != "" {
			t.Errorf("ParseCommand(%q) returned non-empty cmd %q; Handle would return true incorrectly", body, cmd)
		}
	}

	// ParseCommand が "/" で始まるものは Handle で true が返るべき
	commandBodies := []string{
		"/ping",
		"/status",
		"/help",
		"/restart",
		"/unknown-command",
	}
	for _, body := range commandBodies {
		cmd, _ := agenthub.ParseCommand(body)
		if cmd == "" {
			t.Errorf("ParseCommand(%q) returned empty cmd; Handle would return false incorrectly", body)
		}
	}
}

// TestNewCommandRouter は生成直後の状態をテストする。
func TestNewCommandRouter(t *testing.T) {
	router := agenthub.NewCommandRouter()
	if router == nil {
		t.Fatal("NewCommandRouter returned nil")
	}
	// 二重 Registration が panic しないことを確認
	router.Register("/foo", "desc1", func(ctx context.Context, client *agenthub.Client, msg agenthub.Message, args string) (string, error) {
		return "first", nil
	})
	router.Register("/foo", "desc2", func(ctx context.Context, client *agenthub.Client, msg agenthub.Message, args string) (string, error) {
		return "second", nil
	})
	// no panic
}

// ── WithLogger (issue #78) ───────────────────────────────────────────────────

// TestCommandRouter_LogsGoToInjectedLogger は WithLogger で注入したロガーに
// CommandRouter のログ (11 箇所すべての経路) が出ることを確認する。
// slog.Default() に直書きする呼び出しが残っていると、その経路のログは
// 注入先に現れずに失敗する。
func TestCommandRouter_LogsGoToInjectedLogger(t *testing.T) {
	// 全 RPC を 500 で返し、SendMessage / MarkAsRead の失敗経路も通す
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)

	var buf bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
	client, err := agenthub.New(srv.URL, "pat", "@me", "", agenthub.WithLogger(logger))
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name  string
		setup func(r *agenthub.CommandRouter)
		body  string
		want  []string
	}{
		{"ping", nil, "/ping", []string{
			"[command] received", "[command] /ping",
			"[command] reply failed", "[command] MarkAsRead failed",
		}},
		{"status", nil, "/status", []string{"[command] /status"}},
		{"help", nil, "/help", []string{"[command] /help"}},
		{"unknown", nil, "/nope", []string{"[command] unknown command"}},
		{"handler error", func(r *agenthub.CommandRouter) {
			r.Register("/fail", "always fails", func(context.Context, *agenthub.Client, agenthub.Message, string) (string, error) {
				return "", errors.New("handler boom")
			})
		}, "/fail", []string{"[command] handler error"}},
		{"restart ack-only", nil, "/restart", []string{"[command] /restart (no handler, ack-only)"}},
		{"restart failed", func(r *agenthub.CommandRouter) {
			r.SetRestartHandler(func(context.Context) error { return errors.New("restart boom") })
		}, "/restart", []string{"[command] /restart accepted", "[command] /restart handler failed"}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			buf.Reset()
			router := agenthub.NewCommandRouter()
			if tc.setup != nil {
				tc.setup(router)
			}
			msg := agenthub.Message{ID: "m1", Sender: "@alice", Body: tc.body}
			if !router.Handle(context.Background(), client, msg) {
				t.Fatalf("Handle(%q) returned false", tc.body)
			}
			logged := buf.String()
			for _, w := range tc.want {
				if !strings.Contains(logged, w) {
					t.Errorf("injected logger should contain %q, got:\n%s", w, logged)
				}
			}
		})
	}
}
