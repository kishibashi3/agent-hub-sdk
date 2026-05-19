# Changelog

All notable changes to `agent-hub-sdk` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres loosely to [Semantic Versioning](https://semver.org/).

Until `v1.0.0`, breaking changes between minor versions are possible. Each release tag (`vX.Y.Z` on `main`) corresponds to one section below.

## [Unreleased]

### Added — M0 skeleton

- Monorepo bootstrap: `python/` (Python 3.11+) + `js/` (Node 20+ / ESM).
- `python/`: hatchling-based package, `agent_hub_sdk.__version__`, smoke pytest, ruff lint.
- `js/`: TypeScript package with strict tsconfig, `VERSION` export, smoke vitest, eslint.
- `docs/design.md`: full design — modes (`stateful` / `stateless` / `global`), unified `AgentHub.connect` API, layering, monorepo layout, M0–M6 milestones, non-goals.
- `CONTRIBUTING.md`: issue-driven workflow, branch / PR conventions, LGTM-based approval, polyglot-lockstep rule.
- `.github/workflows/ci.yml`: matrix CI (Python 3.11/3.12 × Node 20/22), lint + test + (TS) build, concurrency-cancellation on PR pushes.
- `LICENSE` MIT, root `README.md`.

### Notes

- No PyPI / npm publishing. Install via `pip install git+...` or `npm install github:...`.
- Public API surface is intentionally the bare version constant. M1 lands the real `AgentHub.connect` extracted from `bridge-slack/hub.py`.
