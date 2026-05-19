# Changelog

All notable changes to `agent-hub-sdk` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres loosely to [Semantic Versioning](https://semver.org/).

Until `v1.0.0`, breaking changes between minor versions are possible. Each release tag (`vX.Y.Z` on `main`) corresponds to one section below.

## [Unreleased]

> Section ordering within `[Unreleased]` follows the [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) spec: `Added` → `Changed` → `Deprecated` → `Removed` → `Fixed` → `Security`. Entries within a section may be reverse-chronological.

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

### Changed — Post-M0 governance refinement

- `CONTRIBUTING.md`: documented the post-LGTM amendment rule under `## Reviewing & merging`. New commits after a reviewer's LGTM require a fresh `LGTM ✅` visible on the PR page that covers the amended state; DM acknowledgements do not substitute, and the author cannot tell the reviewer to skip re-verification. Both issue-comment and review-COMMENTED forms count; the merge actor decides which they accept. (Origin: M0 PR #2 procedural-overreach cycle; issue #3.)
- `CONTRIBUTING.md` polish (issue #5): tense-bounded the lax-practice observation to "as of this writing (M0)", added an explicit reference to the 5/24 mutual-review alignment, and pulled out the *"A merge actor reads the PR, not your inbox"* aphorism into a blockquote callout for memorability.
- `CHANGELOG.md`: documented the Keep-a-Changelog 1.1.0 section ordering convention (`Added` → `Changed` → ...) at the top of `[Unreleased]`, and reordered the existing sections accordingly.
