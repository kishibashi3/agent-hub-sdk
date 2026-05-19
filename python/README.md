# agent-hub-sdk (Python)

Python port of [agent-hub-sdk](../README.md). See the root README and [`docs/design.md`](../docs/design.md) for the full picture.

## Install

```bash
pip install git+https://github.com/kishibashi3/agent-hub-sdk.git#subdirectory=python
```

## Status

M0 — skeleton only. `import agent_hub_sdk` works and exposes `__version__`. The real API (`AgentHub.connect`, `inbox`, `send`, ...) lands in M1.

## Development

```bash
cd python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```
