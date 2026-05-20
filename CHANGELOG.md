# Changelog

All notable changes to `agent-hub-sdk` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres loosely to [Semantic Versioning](https://semver.org/).

Until `v1.0.0`, breaking changes between minor versions are possible. Each release tag (`vX.Y.Z` on `main`) corresponds to one section below.

## [Unreleased]

> Section ordering within `[Unreleased]` follows the [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) spec: `Added` → `Changed` → `Deprecated` → `Removed` → `Fixed` → `Security`. Entries within a section may be reverse-chronological.

### Added — M3 stateless mode + `hub.one_shot()` (issue #16)

- `HubSession.one_shot()` — `@asynccontextmanager` that opens a brand-new MCP `ClientSession` over a fresh `streamablehttp_client`, yields a fresh `HubSession` bound to it, and tears it down on exit. The outer session is untouched. Internally delegates to `HubSession.open(self._config)`, so the inner session re-runs `initialize` and registers its own notification handler.
- Stateless pattern documented in `docs/design.md` §3-4: the typical `mode="stateless"` consumer keeps a long-lived `AgentHub.connect` session for the SSE subscribe (= `hub.subscribe_inbox` + `hub.inbox_pushes`) and uses `hub.one_shot()` for each batch of tool calls (= read-then-write pairs against fresh transport).
- `one_shot()` is available regardless of declared `mode`; stateful bridges can still use it to isolate particularly transactional writes from their long-lived session.
- 10 new pytest cases (`tests/test_one_shot.py`): lifecycle (open/yield/close on both normal and exceptional exits), session independence (fresh `_session`, fresh `_status`, isolated push queue), nested use inside `AgentHub.connect`, config passthrough + fail-fast preservation.
- Version bumped 0.3.0 → 0.4.0.

### Added — M2.1 command routing (issue #13, design in #10 Rev 1 + Rev 2)

- `agent_hub_sdk.commands.CommandRouter` — user-extensible dispatch table for `/<verb>` command messages. Pass to `hub.inbox(commands=router)` to intercept commands at the SDK layer.
  - `@router.command(path, *, description=None)` registers a handler with signature `async def fn(msg, hub, args) -> str | None`. Return a string for an auto-reply, `None` for silent (the SDK still acks).
  - `router.passthrough(path, *, description=None)` marks a command as known but yields to the consumer (= LLM-handled).
  - `CommandRouter(builtins=True, unknown="reject", reject_format="command not found: {cmd}")` — built-ins on by default; unknown commands auto-reply with the configured format. Set `unknown="yield"` for chat bridges that want unknown `/foo` to flow to the consumer.
- `parse_command(text)` helper — splits `"/list verbose"` into `("/list", "verbose")`. Same parser the router uses; exposed for bridges that opt out of the router.
- Built-in protocol commands (default `CommandRouter()`):
  - `/ping` → `pong` (plain text). **Behaviour change from M2**: the reply changed from `/pong` to plain `pong` to match `agent-hub#92` Rev 2.
  - `/status` → bridge state. New `HubSession.set_status(value)` API lets bridges update the response (default `"idle"`).
  - `/help` → auto-generated command list with descriptions, gathered from registered handlers + passthroughs + built-ins.
  - Unknown `/foo` → `command not found: /foo` (plain text), configurable via `reject_format`.
- `HubSession.inbox(commands=router, ...)` new kwarg. Backward-compatible with M2 `auto_pong=True` — when `commands=None` (the default) the existing `auto_pong` path still triggers, only the textual reply changes from `/pong` to `pong`.
- 34 new pytest cases (`tests/test_commands.py`): `parse_command` corner cases, registration validation, dispatch precedence (4 states × yield/reject), all built-ins (`/ping` / `/status` / `/help` including override paths), passthrough vs handler precedence, exception swallowing.
- Public exports updated: `CommandRouter`, `CommandHandler`, `DispatchResult`, `parse_command` re-exported from `agent_hub_sdk`.
- Version bumped 0.2.0 → 0.3.0.

### Added — M2 inbox iterator (issue #6 + issue #10)

- `HubSession.inbox(auto_pong=True, poll_interval_s=None, heartbeat_interval_s=60.0)` — async context manager that yields an async iterator merging three concurrent producers into one message stream: (1) the SSE push path via `inbox_pushes()`, (2) a safety-net poll defaulting to 30s (overridable via `AGENT_HUB_INBOX_POLL_INTERVAL_S`), and (3) a `list_tools` heartbeat at 60s. Push and poll share a lock so the same unread batch is never double-processed. Bridges replace the hand-rolled task group + lock + retry from `bridge-claude/worker.py` with a single `async for msg in messages:` loop.
- `/ping` → `/pong` protocol-level handler (issue #10): when `auto_pong=True` (the default), messages whose body equals `"/ping"` (post `str.strip`) are intercepted inside the SDK — a `/pong` reply is sent back, the message is acked, and the iterator does **not** yield it to the caller. Operators can verify a bridge's inbox listener is alive without spending tokens on an LLM round-trip. Opt out with `auto_pong=False`.
- 13 new pytest cases (`tests/test_inbox_iterator.py`): startup-drain, push-driven yield, poll-driven yield, default-on auto-pong intercept, opt-out yields `/ping` to caller, heartbeat fires periodically, dead-session propagation, env-driven poll-interval override, and the underlying `_drain_intercepting_ping` helper (whitespace normalisation, non-`/ping` passthrough, send-failure resilience).
- Public surface: `inbox` is now reachable via `AgentHub.connect(...) → hub.inbox(...)`.

### Added — M1 Python core

- Public ``AgentHub.connect(user, mode="stateful", tenant, ...)`` async context manager. Resolves config from caller args + env (``AGENT_HUB_URL``, ``GITHUB_PAT``, ``AGENT_HUB_TENANT``, ``AGENT_HUB_DISPLAY_NAME``), then opens an MCP streamable-HTTP session against agent-hub.
- ``HubSession`` methods: ``register``, ``send``, ``send_with_retry``, ``get_unread``, ``ack``, ``subscribe_inbox``, ``inbox_pushes``, ``get_participants``, ``heartbeat``. Surface mirrors the existing ``bridge-slack``/``bridge-claude`` ``HubClient`` 1:1 for easy migration.
- Typed dataclasses: ``IncomingMessage``, ``Participant``. Lenient JSON parsers that drop schema-drifted rows rather than crashing the whole batch.
- Error taxonomy: ``ConfigurationError`` (= redline #1 fail-fast at code level, no implicit default URL), ``PeerNotFoundError`` (= send target offline/unregistered, no retry), ``HubTransientError`` (= 5xx/network/timeout, retry-with-backoff). All subclass ``RuntimeError`` for migration compatibility.
- ``classify_hub_error`` patterns covering both English and Japanese agent-hub error strings (e.g. ``存在しません``, ``応答していません``).
- Tests: 70 pytest cases (errors, config, messages, transport, session) with ``unittest.mock`` against the MCP ``ClientSession`` surface.
- Version bumped ``0.0.0`` → ``0.1.0``.

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

### Changed — M1 polish follow-up (issue #9)

- `config.py`: removed the redundant trailing `or None` from the four `resolved_*` fallback chains (`env_map.get(...)` already returns `None` on a missing key). PR #8 review Minor 1.
- `session.py`: `inbox_pushes()` docstring gained a "single-consumer only" note explaining that the underlying memory stream can be iterated by at most one coroutine, and pointing fan-out use cases at `inbox()` instead. PR #8 review Suggestion 1.
- `tests/test_session.py`: new `test_max_attempts_one_means_no_retry` pins the `send_with_retry(max_attempts=1)` intent — a transient on attempt 1 raises without sleeping. PR #8 review Suggestion 5.

### Changed — Post-M0 governance refinement

- `CONTRIBUTING.md`: documented the post-LGTM amendment rule under `## Reviewing & merging`. New commits after a reviewer's LGTM require a fresh `LGTM ✅` visible on the PR page that covers the amended state; DM acknowledgements do not substitute, and the author cannot tell the reviewer to skip re-verification. Both issue-comment and review-COMMENTED forms count; the merge actor decides which they accept. (Origin: M0 PR #2 procedural-overreach cycle; issue #3.)
- `CONTRIBUTING.md` polish (issue #5): tense-bounded the lax-practice observation to "as of this writing (M0)", added an explicit reference to the 5/24 mutual-review alignment, and pulled out the *"A merge actor reads the PR, not your inbox"* aphorism into a blockquote callout for memorability.
- `CHANGELOG.md`: documented the Keep-a-Changelog 1.1.0 section ordering convention (`Added` → `Changed` → ...) at the top of `[Unreleased]`, and reordered the existing sections accordingly.
