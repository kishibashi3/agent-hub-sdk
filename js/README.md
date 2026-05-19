# @kishibashi3/agent-hub-sdk (TypeScript)

TypeScript port of [agent-hub-sdk](../README.md). See the root README and [`docs/design.md`](../docs/design.md) for the full picture.

## Install

```bash
npm install github:kishibashi3/agent-hub-sdk
```

The package lives in the `js/` subdirectory of the repo. npm's `github:` resolver picks it up via the `repository.directory` field in `package.json`.

## Status

M0 — skeleton only. `import { VERSION } from "@kishibashi3/agent-hub-sdk"` works. The real API (`AgentHub.connect`, `inbox`, `send`, ...) lands in M4 after the Python core is solid.

## Development

```bash
cd js
npm install
npm test
npm run typecheck
npm run lint
```

## Runtime support

Node 20+. No browser support is planned for v0.x.
