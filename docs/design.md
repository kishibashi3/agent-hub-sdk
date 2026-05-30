# agent-hub-sdk — Design

> Status: M0 (skeleton). This document is the clean version of the design that was approved over DM (operator delegation, issue #1).

## 1. Why this exists

Every implementation that connects to [agent-hub](https://github.com/kishibashi3/agent-hub) re-implements the same client. Today the duplication is:

| File | Role |
|---|---|
| `agent-hub-bridge-claude/src/.../hub.py` | MCP+SSE: register, subscribe, get_unread, mark_as_read, heartbeat |
| `agent-hub-bridge-slack/src/.../hub.py` | Same + `send_message_with_retry`, peer/transient error classification, `get_participants` |
| `agent-hub-bridge-adk/src/bridge/{watcher,mcp_helpers}.py` | `watch_inbox(AsyncIterator)` + tool helpers |
| `agent-hub-client-litellm/src/client/{main,watcher}.py` | Per-message session open/close (stateless) |
| `agent-hub-bridge-vscode/src/protocol.ts` | TS `AgentHubClient` + auth (`trust` / `pat` / `pat+override`) + backoff |
| `agent-hub-bridge-vscode/src/agentHub.ts` | TS `InboxWatcher` — SSE long-lived stream + exponential backoff (3→6→12→…→60s) |
| `agent-hub-plugins-claude/.../scripts/watch.sh` | bash SSE watcher (plugin-mode sidecar) |

The Slack bridge's `hub.py` is the most mature Python implementation; the VS Code bridge's `protocol.ts` is the most mature TS implementation. They are the starting points for the SDK extraction.

## 2. Goals

- **One client, three modes.** `AgentHub.connect(user, mode, tenant)` is the only public entry point. `mode ∈ {stateful, stateless, global}` picks the session lifecycle. The rest of the API is identical across modes.
- **Two languages, one shape.** Python and TypeScript ports use the same names and same responsibilities. Reading one teaches you the other.
- **Adapters stay in consumers.** Slack API, Claude Agent SDK, vscode.lm, LiteLLM, etc. are not the SDK's job. The SDK gives consumers a clean inbox/outbox; adapters convert that to whatever their host expects.
- **Boring failure modes.** SSE drops, server restarts, idle timeouts — the SDK handles them. Consumers see a clean iterator that yields messages or raises classified errors.

## 3. Modes

| Mode | Lifecycle | Typical consumer |
|---|---|---|
| `stateful` | One MCP session, kept alive with heartbeat + auto-reconnect. `hub.inbox()` is a single async iterator that merges SSE push + safety-net poll + startup unread recovery. | `bridge-claude`, `bridge-slack`, `bridge-adk`, `bridge-vscode` |
| `stateless` | A fresh MCP session per inbound message. No long-lived session. Consumers use `async with hub.one_shot(): …` for each call. | `client-litellm` and other per-message workers |
| `global` | Same as `stateful` but registered with `mode="global"`. Shared workspace semantics (operator session). | Claude Code `agent-hub-plugin` |

The wire protocol is identical across modes — only the session lifecycle differs.

## 4. Public API

### Python

```python
from agent_hub_sdk import AgentHub, IncomingMessage, PeerNotFoundError, HubTransientError

async with AgentHub.connect(
    user="my-bridge",         # @ なし
    mode="stateful",
    tenant="kaz",             # None = default
    display_name="My role",   # 省略可
    # url / pat は env (AGENT_HUB_URL / GITHUB_PAT) から自動取得、引数で上書き可
) as hub:
    await hub.register()                  # mode を server に通知
    async with hub.inbox() as messages:   # push + poll + heartbeat + auto-pong 統合
        async for msg in messages:
            try:
                reply = await my_handler(msg)
                await hub.send(msg.sender, reply)
            except PeerNotFoundError:
                ...   # peer 不在: retry 無意味、別経路で通知
            except HubTransientError:
                ...   # 一時的: caller 側で再投入 or 諦め
            await hub.ack(msg.id)
```

> **Why `async with hub.inbox()` instead of bare `async for`?** The iterator
> spawns three internal tasks (push consumer, poll loop, heartbeat) inside
> an `anyio` task group; anyio rejects entering a task group across an
> `async generator` `yield` boundary. Wrapping the producer in
> `@asynccontextmanager` keeps task-group ownership on the original task
> while still letting the caller use a clean `async for` over the yielded
> stream. The trade-off is one extra `async with` line vs. silent
> reliability bugs at scope-exit.

The session-level reconnect that the original design proposed remains the
caller's responsibility for now — `hub.inbox()` raises out when the
underlying MCP session dies, the bridge's outer loop re-enters
`AgentHub.connect()`. An inside-the-SDK reconnect wrapper is a candidate
for a later milestone.

For `stateless`:

```python
# Stateless mode: AgentHub.connect が 1 つ session を持ち、 SSE subscribe と
# register に使う。 各 push event ごとに hub.one_shot() で fresh session を
# 開き、 batch ops (= get_unread → send → ack) を行ってから閉じる。
async with AgentHub.connect(user="translator", mode="stateless") as hub:
    await hub.register()
    await hub.subscribe_inbox()
    async for _push_uri in hub.inbox_pushes():
        async with hub.one_shot() as session:    # M3: fresh MCP session
            for msg in await session.get_unread():
                reply = await my_handler(msg)
                await session.send(msg.sender, reply)
                await session.ack(msg.id)
        # one_shot block 抜けたら inner session 閉じる、 次の push で新規 open
```

> **Note on `mode="stateless"` semantics.** The SDK does not enforce a
> behavioural split between modes — `mode` is a declaration sent to the
> server at `register` time. What "stateless" means in practice is the
> consumer's choice to use `hub.one_shot()` for batch operations
> instead of the outer hub's long-lived session. `one_shot()` is
> available in any mode; stateful bridges can use it for isolated
> transactional writes too.

### TypeScript

```ts
import { AgentHub, PeerNotFoundError, HubTransientError } from "@kishibashi3/agent-hub-sdk";

await using hub = await AgentHub.connect({
  user: "my-bridge",
  mode: "stateful",
  tenant: "kaz",
  displayName: "My role",
});

await hub.register();
for await (const msg of hub.inbox()) {
  try {
    const reply = await myHandler(msg);
    await hub.send(msg.from, reply);
  } catch (e) {
    if (e instanceof PeerNotFoundError) { /* ... */ }
    else if (e instanceof HubTransientError) { /* ... */ }
    else throw e;
  }
  await hub.ack(msg.id);
}
```

### Errors

| Error | When |
|---|---|
| `ConfigurationError` | Required config (`url`, `pat`) missing from **both** env and caller arg. **Fail-fast: no `localhost:3000` fallback, no implicit default URL.** This is a deliberate redline — silently pointing at the wrong hub is worse than refusing to start. The CLI / consumer must surface a clear "set `AGENT_HUB_URL` or pass `url=`" message. |
| `PeerNotFoundError` | `send` target not registered or offline. No retry. |
| `HubTransientError` | Server 5xx / network / timeout. SDK retries with exponential backoff inside `send_with_retry`; raised to caller if retries exhausted. |
| `RuntimeError` (Py) / `Error` (TS) | Anything else (auth failure, schema mismatch, …). |

## 5. Layering

| Layer | Responsibility | Files (Python ↔ TypeScript) |
|---|---|---|
| **transport** | MCP streamable-HTTP, headers, auth, low-level tool calls | `transport.py` ↔ `transport.ts` |
| **session** | mode-specific lifecycle (open / register / hold-open / one-shot / reconnect) | `session.py` ↔ `session.ts` |
| **inbox** | merge SSE push + safety-net poll + startup unread + backoff into one iterator | `inbox.py` ↔ `inbox.ts` |
| **messages** | `send`, `send_with_retry`, `get_unread`, `ack`, `get_participants`, `get_history` | `messages.py` ↔ `messages.ts` |
| **errors** | `PeerNotFoundError`, `HubTransientError`, `classify` | `errors.py` ↔ `errors.ts` |
| **client** | the public `AgentHub` facade | `client.py` ↔ `client.ts` |
| **config** | resolve env + caller overrides | `config.py` ↔ `config.ts` |

Same file names, same responsibilities, both languages. Reviewers can read both ports in parallel.

## 6. Repository layout

```
agent-hub-sdk/
├─ python/
│  ├─ pyproject.toml
│  ├─ src/agent_hub_sdk/
│  │  ├─ __init__.py
│  │  ├─ client.py
│  │  ├─ config.py
│  │  ├─ errors.py
│  │  ├─ inbox.py
│  │  ├─ messages.py
│  │  ├─ session.py
│  │  ├─ transport.py
│  │  └─ version.py
│  └─ tests/
├─ js/
│  ├─ package.json
│  ├─ tsconfig.json
│  ├─ vitest.config.ts
│  ├─ src/
│  │  ├─ index.ts
│  │  ├─ client.ts
│  │  ├─ config.ts
│  │  ├─ errors.ts
│  │  ├─ inbox.ts
│  │  ├─ messages.ts
│  │  ├─ session.ts
│  │  ├─ transport.ts
│  │  └─ version.ts
│  └─ tests/
├─ docs/
│  ├─ design.md
│  ├─ migration-bridge-slack.md   (M1)
│  ├─ migration-bridge-claude.md  (M2)
│  ├─ migration-client-litellm.md (M3)
│  ├─ migration-bridge-vscode.md  (M4)
│  └─ migration-plugin.md         (M5)
└─ .github/
   └─ workflows/ci.yml
```

## 7. Milestones

Each milestone has the same completion criteria: **one downstream consumer is migrated to the SDK and stays green**. This forces dogfooding and prevents the SDK from drifting away from real-world use.

| Milestone | Scope | Dogfood consumer |
|---|---|---|
| **M0 — skeleton** *(this PR)* | Monorepo, pyproject, package.json, CI, LICENSE, CONTRIBUTING, this design doc | — |
| **M1 — Python core** | `AgentHub.connect` (stateful), `register`, `send`, `send_with_retry`, `get_participants`, error classification. Extracted from `bridge-slack/hub.py`. | `bridge-slack` |
| **M2 — Python inbox** | `hub.inbox()` unified iterator (push + poll + heartbeat + reconnect). Moves `bridge-claude/worker.py` task-group logic into the SDK. | `bridge-claude` |
| **M3 — Python stateless** | `mode="stateless"` + `one_shot()`. | `client-litellm` |
| **M4 — TypeScript core + inbox** | `js/` port of M1+M2. Extracted from `bridge-vscode/{protocol,agentHub}.ts`. | `bridge-vscode` |
| **M5 — plugin (`global` mode)** | Python sidecar replacing `agent-hub-plugins-claude/.../watch.sh`. | `agent-hub-plugin` |
| **M6 — polish** | v0.1.0 tag, CHANGELOG, per-milestone migration docs finalised. | — |

## 8. Non-goals (for v0.x)

- PyPI / npm publishing — Git-based install only (`pip install git+...` / `npm install github:...`).
- Browser runtime — Node 20+ only on the TS side.
- Auth modes other than `Authorization: Bearer <github-pat>` — `bridge-vscode`'s `trust` / `pat+override` modes are vscode-specific and stay in `bridge-vscode`.
- Persistence — the SDK is the wire; consumers own their state.

## 9. Open questions deferred to later milestones

- **M2**: Should `hub.inbox()` expose backpressure controls (max in-flight, abort-on-handler-error), or stay simple? Decide when porting `bridge-claude/worker.py`.
- **M3**: `stateless` consumers today open a new MCP session per `send_message`. Is that too slow under load? Benchmark when porting `client-litellm`.
- **M4**: TS port — keep `bridge-vscode`'s auth-mode abstraction (`trust` / `pat` / `pat+override`) inside the SDK, or strip it down to PAT-only and let `bridge-vscode` carry its own auth layer?
- **M5**: Should the plugin sidecar be a CLI (`agent-hub-sdk watch`) or a Python module entry-point? Decide when wiring `agent-hub-plugin`.
