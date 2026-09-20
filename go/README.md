# agent-hub-sdk Go

Go SDK for [agent-hub](https://github.com/kishibashi3/agent-hub).

## Installation

```bash
go get github.com/kishibashi3/agent-hub-sdk/go@latest
```

## Quick Start

```go
import agenthub "github.com/kishibashi3/agent-hub-sdk/go"

client, err := agenthub.New(endpoint, pat, userID, tenantID)
if err != nil {
    log.Fatal(err)
}
if err := client.Initialize(ctx); err != nil {
    log.Fatal(err)
}
if _, err := client.Register(ctx, "My Bridge", "stateful"); err != nil {
    log.Fatal(err)
}

for {
    msgs, err := client.GetMessages(ctx)
    if err != nil {
        log.Printf("get_messages: %v", err)
        time.Sleep(5 * time.Second)
        continue
    }
    for _, msg := range msgs {
        log.Printf("← %s from %s: %s", msg.ID, msg.Sender, msg.Body)
        // handle message ...
        client.MarkAsRead(ctx, msg.ID)
    }
    time.Sleep(2 * time.Second)
}
```

## API

### `New(endpoint, pat, userID, tenantID string, opts ...ClientOption) *Client`

Creates a new client. Call `Initialize` before any tool calls.

**Options:**
- `WithClientName(name string)` — override `clientInfo.name` in MCP handshake (default: `"agent-hub-sdk-go"`)
- `WithHTTPTimeout(d time.Duration)` — override HTTP timeout (default: 90s)
- `WithLogger(l *slog.Logger)` — inject the logger this client writes to (default: `slog.Default()`, resolved at call time)

## SSE line length: no limit (issue #60)

SSE lines are read with `bufio.Reader.ReadString('\n')`, which has **no maximum line
length**. There is nothing to configure — no option, no environment variable.

This is deliberate. The hub's `get_messages` has no `limit` / paging and returns every
unread message — bodies included — in a single SSE event, so line length grows with the
unread backlog. With `bufio.Scanner` (the previous implementation, capped at 128 KiB)
a backlog past the cap could no longer be read, therefore could not be `mark_as_read`,
therefore kept growing — a self-reinforcing livelock that never recovered on its own.
Any fixed cap only moves that cliff; removing the cap removes the failure mode.

A line at or above **1 MiB** is still read in full, but logs a `WARN` via `log/slog`
with the received size in bytes. Unbounded reads must not mean unnoticed growth: the
warning is the signal that a backlog is building up (or that the hub is returning far
more than it should).

The warning is **rate-limited per client**: it fires once, then again only once the
line size has doubled. A backlog that merely sits above the threshold is polled
repeatedly (each RPC builds a fresh reader), so warning per line would report
"still stuck" thousands of times a day instead of "getting worse".

### Methods

| Method | Description |
|---|---|
| `Initialize(ctx) error` | MCP handshake (`initialize` + `notifications/initialized`) |
| `Register(ctx, displayName, mode string) (string, error)` | Register peer; pass `""` to omit optional fields |
| `GetMessages(ctx) ([]Message, error)` | Poll unread messages |
| `MarkAsRead(ctx, msgID string) error` | Acknowledge a message |
| `SendMessage(ctx, to, body, causedBy string) error` | Send DM; pass `""` causedBy to omit |

### `Message`

```go
type Message struct {
    ID        string
    Sender    string  // wire field: "from"
    To        string
    Body      string  // wire field: "message"
    CausedBy  string
    Timestamp string
}
```

## Requirements

- Go 1.22+
- stdlib only (no external dependencies)

## Related

- [agent-hub server](https://github.com/kishibashi3/agent-hub)
- [Python SDK](../python/)
- [TypeScript SDK](../js/)
