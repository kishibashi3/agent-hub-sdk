// command_router.go — Go SDK CommandRouter (issue #43)
//
// Python SDK の CommandRouter (/ping /status /help /restart の組み込み対応 +
// カスタムコマンド登録) を Go で実装する。
//
// 基本的な使い方:
//
//	router := agenthub.NewCommandRouter()
//	router.SetStatusFunc(tracker.Status)
//	router.SetRestartHandler(func(ctx context.Context) error {
//	    return runner.Restart(ctx)
//	})
//	router.Register("/active", "現在のタスク", func(ctx context.Context, client *agenthub.Client, msg agenthub.Message, args string) (string, error) {
//	    return currentTask(), nil
//	})
//
//	for _, msg := range msgs {
//	    if router.Handle(ctx, client, msg) {
//	        continue  // コマンドとして処理済み (MarkAsRead 呼び済み)
//	    }
//	    // 自然言語 DM を処理する
//	}
//
// # 組み込みコマンド
//
//   - /ping    → "pong" を返信 (health check)
//   - /status  → statusFn() の結果を返信 (デフォルト "idle")
//   - /help    → 登録コマンド一覧を返信
//   - /restart → 2 段階返信: "restarting..." → restartFn(ctx) → "ready"
//     restartFn が nil なら ack-only (返信なし)
//
// # Handle の戻り値
//
//   - true  → コマンドとして処理した。MarkAsRead 呼び済み。caller は continue するだけ。
//   - false → 自然言語 DM。caller が通常処理を続ける。
package agenthub

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
)

// CommandHandler はカスタムコマンドハンドラの関数型。
//
//   - (reply, nil) を返すと reply を送信元に送信して MarkAsRead する。
//     reply が空文字列の場合は送信しない (ack-only)。
//   - ("", nil) を返すと返信なしで MarkAsRead する。
//   - (_, err) を返すと警告メッセージを送信元に送り MarkAsRead する。
//     エラーは bridge log に記録されるが inbox ループには伝播しない。
type CommandHandler func(ctx context.Context, client *Client, msg Message, args string) (string, error)

// RestartHandler は /restart コマンドの callback 関数型。
// bridge のセッションリセット処理を実装する。nil を設定すると /restart は ack-only になる。
type RestartHandler func(ctx context.Context) error

// CommandRouter は "/" プレフィックスのコマンドメッセージをディスパッチする。
//
// ゼロ値は使用不可。必ず NewCommandRouter() で生成すること。
// スレッドセーフではない。メッセージ処理の単一 goroutine からのみ呼ぶこと。
type CommandRouter struct {
	statusFn     func() string
	restartFn    RestartHandler
	handlers     map[string]CommandHandler
	descriptions map[string]string
	handlerOrder []string // /help 生成時の登録順を保持
}

// NewCommandRouter は CommandRouter を生成する。
// 組み込みコマンド (/ping /status /help /restart) はデフォルトで有効。
func NewCommandRouter() *CommandRouter {
	return &CommandRouter{
		statusFn:     func() string { return "idle" },
		handlers:     make(map[string]CommandHandler),
		descriptions: make(map[string]string),
	}
}

// SetStatusFunc は /status コマンドのステータス返却関数を登録する。
// 未設定時は "idle" を返す。
// 用途: bridge の activity tracker を使って "busy"/"idle" を動的に返す。
//
//	router.SetStatusFunc(tracker.Status)
func (r *CommandRouter) SetStatusFunc(fn func() string) {
	r.statusFn = fn
}

// SetRestartHandler は /restart コマンドの callback を登録する。
// fn が nil の場合は /restart は ack-only (返信なし) になる。
// fn が非 nil の場合は 2 段階返信:
//  1. "restarting..." を送信元に送信
//  2. fn(ctx) を呼び出す (bridge のセッションリセット等)
//  3. 成功なら "ready"、失敗なら警告メッセージを送信
func (r *CommandRouter) SetRestartHandler(fn RestartHandler) {
	r.restartFn = fn
}

// Register はカスタムコマンドハンドラを登録する。
// cmd は "/" で始まる必要がある (例: "/active")。
// 同じ cmd を再登録すると上書きされる。
// 組み込みコマンド (/ping /status /help /restart) を上書き可能。
func (r *CommandRouter) Register(cmd, description string, fn CommandHandler) {
	if !strings.HasPrefix(cmd, "/") {
		panic(fmt.Sprintf("CommandRouter.Register: cmd must start with '/' (got %q)", cmd))
	}
	if _, exists := r.handlers[cmd]; !exists {
		r.handlerOrder = append(r.handlerOrder, cmd)
	}
	r.handlers[cmd] = fn
	r.descriptions[cmd] = description
}

// ParseCommand は message body を (command, args) に分割する。
//
//   - "/" で始まらない場合、または body が "/" だけの場合は ("", "") を返す。
//   - "/ping" → ("/ping", "")
//   - "/list verbose" → ("/list", "verbose")
//   - "  /status  " → ("/status", "")
func ParseCommand(body string) (cmd, args string) {
	trimmed := strings.TrimSpace(body)
	if !strings.HasPrefix(trimmed, "/") || trimmed == "/" {
		return "", ""
	}
	parts := strings.SplitN(trimmed, " ", 2)
	cmd = parts[0]
	if len(parts) == 2 {
		args = strings.TrimSpace(parts[1])
	}
	return cmd, args
}

// Handle は 1 件のメッセージをルーティングする。
//
// コマンドメッセージ ("/" で始まる body) の場合:
//   - 適切なハンドラを呼び出して返信を送信する。
//   - client.MarkAsRead(ctx, msg.ID) を呼ぶ。
//   - true を返す。caller は continue するだけでよい。
//
// コマンドでないメッセージ (自然言語 DM) の場合:
//   - false を返す。MarkAsRead は呼ばない。caller が処理を続ける。
//
// ディスパッチ優先順位:
//  1. Register で登録したカスタムハンドラ
//  2. 組み込みハンドラ (/ping /status /help /restart)
//  3. 未知コマンド → "command not found: /foo" を返信
func (r *CommandRouter) Handle(ctx context.Context, client *Client, msg Message) bool {
	cmd, args := ParseCommand(msg.Body)
	if cmd == "" {
		return false
	}

	slog.Debug("[command] received", "cmd", cmd, "from", msg.Sender, "msg_id", msg.ID)

	// 1. カスタムハンドラ優先
	if fn, ok := r.handlers[cmd]; ok {
		r.runCustomHandler(ctx, client, msg, cmd, args, fn)
	} else {
		// 2. 組み込みハンドラ
		switch cmd {
		case "/ping":
			slog.Info("[command] /ping", "from", msg.Sender)
			r.sendReply(ctx, client, msg, "pong")
		case "/status":
			status := r.statusFn()
			slog.Info("[command] /status", "from", msg.Sender, "status", status)
			r.sendReply(ctx, client, msg, status)
		case "/help":
			slog.Info("[command] /help", "from", msg.Sender)
			r.sendReply(ctx, client, msg, r.generateHelp())
		case "/restart":
			r.runRestart(ctx, client, msg)
		default:
			// 3. 未知コマンド
			slog.Info("[command] unknown command", "cmd", cmd, "from", msg.Sender)
			r.sendReply(ctx, client, msg, fmt.Sprintf("command not found: %s", cmd))
		}
	}

	if err := client.MarkAsRead(ctx, msg.ID); err != nil {
		slog.Warn("[command] MarkAsRead failed", "msg_id", msg.ID, "err", err)
	}
	return true
}

// sendReply は msg.Sender に text を送信する。
// 失敗した場合は警告ログのみ (inbox ループを落とさない)。
// causedBy に msg.ID を設定して因果チェーンを繋げる。
func (r *CommandRouter) sendReply(ctx context.Context, client *Client, msg Message, text string) {
	if text == "" {
		return
	}
	if err := client.SendMessage(ctx, msg.Sender, text, msg.ID); err != nil {
		slog.Warn("[command] reply failed", "cmd_preview", truncateCmd(msg.Body), "err", err)
	}
}

// runCustomHandler はユーザー登録ハンドラを呼び出す。
// handler が error を返した場合は警告メッセージを送信する。
func (r *CommandRouter) runCustomHandler(
	ctx context.Context, client *Client, msg Message,
	cmd, args string, fn CommandHandler,
) {
	reply, err := fn(ctx, client, msg, args)
	if err != nil {
		slog.Warn("[command] handler error", "cmd", cmd, "from", msg.Sender, "err", err)
		r.sendReply(ctx, client, msg, fmt.Sprintf("⚠ command %s failed: see bridge log", cmd))
		return
	}
	if reply != "" {
		r.sendReply(ctx, client, msg, reply)
	}
}

// runRestart は組み込み /restart コマンドを実行する。
// 2 段階プロトコル: "restarting..." → restartFn(ctx) → "ready"
// restartFn が nil の場合は ack-only (返信なし)。
func (r *CommandRouter) runRestart(ctx context.Context, client *Client, msg Message) {
	if r.restartFn == nil {
		slog.Info("[command] /restart (no handler, ack-only)", "from", msg.Sender)
		return
	}

	slog.Info("[command] /restart accepted", "from", msg.Sender)
	r.sendReply(ctx, client, msg, "restarting...")

	if err := r.restartFn(ctx); err != nil {
		slog.Warn("[command] /restart handler failed", "err", err)
		r.sendReply(ctx, client, msg, "⚠ /restart failed: see bridge log")
		return
	}

	r.sendReply(ctx, client, msg, "ready")
}

// generateHelp は登録コマンドの一覧テキストを生成する。
// 順序: 組み込みコマンド (カスタム上書きがないもの) → カスタムハンドラ (登録順)
func (r *CommandRouter) generateHelp() string {
	var lines []string
	emitted := make(map[string]bool)

	// 組み込みコマンド (カスタムハンドラで上書きされていないもの)
	builtins := []struct{ cmd, desc string }{
		{"/ping", "health check"},
		{"/status", "bridge state (idle/busy)"},
		{"/help", "show this help"},
		{"/restart", "reset the bridge's session context"},
	}
	for _, b := range builtins {
		if r.handlers[b.cmd] != nil {
			continue // カスタムハンドラがあれば後のブロックで出力
		}
		lines = append(lines, fmt.Sprintf("%-12s — %s", b.cmd, b.desc))
		emitted[b.cmd] = true
	}

	// カスタムハンドラ (登録順)
	for _, cmd := range r.handlerOrder {
		if emitted[cmd] {
			continue
		}
		desc := r.descriptions[cmd]
		lines = append(lines, fmt.Sprintf("%-12s — %s", cmd, desc))
		emitted[cmd] = true
	}

	if len(lines) == 0 {
		return "(no commands registered)"
	}
	return strings.Join(lines, "\n")
}

// truncateCmd は コマンド body のプレビュー文字列を返す (ログ用)。
func truncateCmd(body string) string {
	const maxLen = 40
	if len(body) <= maxLen {
		return body
	}
	return body[:maxLen] + "..."
}
