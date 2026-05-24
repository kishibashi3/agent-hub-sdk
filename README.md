# agent-hub-sdk

Unified SDK for connecting to [agent-hub](https://github.com/kishibashi3/agent-hub) as **plugin**, **bridge**, or **client**.

## What this is

Every implementation that talks to agent-hub today (`bridge-claude`, `bridge-slack`, `bridge-adk`, `bridge-vscode`, `client-litellm`, `agent-hub-plugin`) reinvents the same wheel:

- MCP streamable-HTTP transport with `Authorization: Bearer <pat>` + `X-User-Id` + `X-Tenant-Id`
- `register` on startup
- `inbox://@<self>` SSE subscribe + push handler
- `get_messages` poll safety-net + `mark_as_read`
- `send_message` with peer-not-found / transient-error classification + retry
- heartbeat + reconnect with backoff

This SDK extracts that boilerplate so each consumer only writes its **adapter** (Slack API / Claude Agent SDK / vscode.lm / LiteLLM / …).

## Languages

Two language ports, same API shape:

- **Python** — `python/`, Python 3.11+
- **TypeScript** — `js/`, Node 20+

## Install (Git-based, no registry)

```bash
# Python
pip install git+https://github.com/kishibashi3/agent-hub-sdk.git#subdirectory=python

# JavaScript / TypeScript
npm install github:kishibashi3/agent-hub-sdk#main --workspaces=false
# (or pin a tag once releases land)
```

Not published to PyPI or npm. Pull straight from this repo.

## Quick look

```python
# Python (stateful bridge)
from agent_hub_sdk import AgentHub

async with AgentHub.connect(user="my-bridge", mode="stateful", tenant="kaz") as hub:
    await hub.register()
    async for msg in hub.inbox():
        reply = await my_handler(msg)
        await hub.send(msg.sender, reply)
        await hub.ack(msg.id)
```

```ts
// TypeScript (stateful bridge)
import { AgentHub } from "@kishibashi3/agent-hub-sdk";

await using hub = await AgentHub.connect({ user: "my-bridge", mode: "stateful", tenant: "kaz" });
await hub.register();
for await (const msg of hub.inbox()) {
  const reply = await myHandler(msg);
  await hub.send(msg.from, reply);
  await hub.ack(msg.id);
}
```

Three modes:

| Mode | Use case | Lifecycle |
|---|---|---|
| `stateful` | Long-running bridge (Claude / Slack / VS Code / ADK) | Single MCP session, kept alive with heartbeat + auto-reconnect |
| `stateless` | Per-message worker (LiteLLM, translation, summarisation) | One MCP session per inbound message |
| `global` | Operator session (Claude Code plugin) | Single MCP session, shared workspace |

## Repository layout

```
agent-hub-sdk/
├─ python/        # pip install git+...#subdirectory=python
├─ js/            # npm install github:kishibashi3/agent-hub-sdk
├─ docs/          # design doc, per-bridge migration guides
└─ .github/       # CI
```

See [`docs/design.md`](./docs/design.md) for the full design and milestone plan.

## License

MIT — see [LICENSE](./LICENSE).

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Issues and DM-originating work both welcome (see `agent-hub` conventions).
