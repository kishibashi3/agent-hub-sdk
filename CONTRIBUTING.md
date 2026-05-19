# Contributing to agent-hub-sdk

Thanks for looking. This SDK powers every implementation that talks to [agent-hub](https://github.com/kishibashi3/agent-hub), so changes here ripple to every bridge and plugin downstream. Read this first.

## Workflow

### Issue-driven (preferred)

Every change starts with an [issue](https://github.com/kishibashi3/agent-hub-sdk/issues). New features, design changes, and bug fixes all begin there — no PR without an issue.

### DM-originating (alternative)

Cross-peer co-design and operator delegation are accepted as substitute origins. In that case the PR body must include an **"依頼元 / Origin"** field pointing at the originating DM id or delegation context. This matches the `agent-hub` ecosystem convention.

### When in doubt

Don't guess. Ask the requester before implementing.

## Branching & PRs

- Feature branches are named `<milestone>/<short-slug>` (e.g. `m1/python-extract`, `m2/python-inbox`).
- One PR per logical change. Keep them small enough that a reviewer can read the diff in one sitting.
- PR title: `<scope>: <short summary>` (e.g. `python: extract HubClient from bridge-slack`).
- PR body: include the issue link (or origin DM) and a brief "what changed / why".

## Reviewing & merging

All peers in this ecosystem share a single GitHub identity, so formal `gh pr review --approve` is not available. Instead:

1. A reviewer posts **`LGTM ✅`** on the PR page to signal approval (see [Post-LGTM amendments](#post-lgtm-amendments) below for which forms count).
2. The PR can then be merged. Who merges depends on scope:
   - **Revert-safe** (docs, refactors, new features, tests, skeleton): `@planner` self-merges after `LGTM ✅` (L0).
   - **Breaking** (back-compat breaks, API contract change, DB migration, secret/deploy change): escalate to `@ope-ultp1635` who merges as L1.
   - **Author**: do not self-merge your own PR. Route through planner or operator.

### Post-LGTM amendments

If a reviewer has posted `LGTM ✅` and the PR is then amended (any new commit pushed), the merge actor (planner / operator) **must** see a fresh `LGTM ✅` on the PR page that covers the amended state.

- "On the PR page" includes both an issue-comment (via `gh pr comment` or the GitHub UI's *Add a comment* box) **and** a review with `COMMENTED` state (via `gh pr review --comment`). The merge actor decides which forms they accept; ecosystem practice today leans lax (review-form is routinely accepted).
- DM acknowledgements from the reviewer do **not** substitute for the PR-visible LGTM. A merge actor reads the PR, not your inbox.
- Authors **must not** tell the reviewer "re-LGTM is unnecessary" — that judgement belongs to the merge actor, not the author. Asking the reviewer to skip the formal step is asking them to bypass governance on your behalf.
- If the amendment is purely procedural (typo, lockfile bump, ground-truth fixups the reviewer themselves requested, etc.) the reviewer may post a one-line `LGTM ✅ (re-verify of <sha>)` immediately. Depth is optional; PR-page visibility is mandatory.

The rule exists because GitHub treats LGTM as evidence tied to a specific commit hash. Silently letting subsequent commits ride on stale approval defeats the purpose of review.

## Languages

This is a polyglot monorepo. Two ports of the same API live side by side:

- **Python** in `python/` — Python 3.11+
- **TypeScript** in `js/` — Node 20+

Keep them in lockstep when feasible. If an API is added on one side, file a follow-up issue for the other side and link both PRs.

## Local dev

### Python

```bash
cd python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

### TypeScript

```bash
cd js
npm install
npm test
npm run lint
npm run typecheck
```

CI runs the same commands on every PR.

## Style

- Python: `ruff` for lint + format, `pytest` for tests. Type hints required on public API.
- TypeScript: `eslint` + `prettier`, `vitest` for tests. No `any` on public API.
- Docstrings: explain **why**, not what. The diff already shows what.
- Comments in Japanese or English both fine — match surrounding file.

## Releases

No PyPI / npm publishing. Consumers install directly from GitHub:

```bash
pip install git+https://github.com/kishibashi3/agent-hub-sdk.git#subdirectory=python
npm install github:kishibashi3/agent-hub-sdk
```

Tag releases as `vX.Y.Z` on `main`. Consumers can pin a tag.

## License

By contributing you agree your contributions are licensed under the MIT License (see [LICENSE](./LICENSE)).
