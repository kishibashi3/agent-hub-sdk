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
- `WithSSEMaxLineBytes(n int)` — override the SSE per-line size limit (default: 8 MiB); takes precedence over the environment variable below

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_HUB_SDK_SSE_MAX_LINE_BYTES` | `8388608` (8 MiB) | Maximum size of a single SSE line (= one event's `data`). Minimum `65536` (64 KiB). |

Unset or empty → the default is used silently. **Set to an invalid value (non-integer, or below 64 KiB) → `New()` returns an error (fail-fast).** Invalid values are never silently replaced by the default: a size limit that is not in effect is exactly the failure mode this setting exists to prevent.

> ⚠️ **Raising the limit is mitigation, not a fix** (issue #60). The hub's `get_messages` has no `limit` / paging and returns every unread message — bodies included — in a single response. Once that response exceeds the limit, the client can no longer read it, therefore cannot `mark_as_read`, therefore accumulates more unread messages — a self-reinforcing livelock that does not recover on its own. No value of this limit removes that structure; the real fix is paging in the hub.

Only the Go SDK has such a limit. The Python and TypeScript SDKs read SSE through the official MCP SDK (`httpx-sse` / MCP TS SDK), which imposes no fixed per-line cap.

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
