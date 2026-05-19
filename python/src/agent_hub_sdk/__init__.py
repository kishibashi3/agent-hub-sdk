"""agent-hub-sdk — Unified Python SDK for agent-hub.

Status: M0 (skeleton). Public surface is intentionally minimal — only the
version constant is exported. M1 will add `AgentHub`, error classes, and
`IncomingMessage` after extracting them from `bridge-slack/hub.py`.

See `docs/design.md` for the full plan.
"""

from agent_hub_sdk.version import __version__

__all__ = ["__version__"]
