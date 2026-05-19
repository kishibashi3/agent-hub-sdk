"""agent-hub-sdk — Unified Python SDK for agent-hub.

Status: M1 (Python core, stateful mode). See ``docs/design.md`` for the full
roadmap and ``CHANGELOG.md`` for what's landed.

Typical usage::

    from agent_hub_sdk import AgentHub, PeerNotFoundError, HubTransientError

    async with AgentHub.connect(user="my-bridge", mode="stateful") as hub:
        await hub.register()
        try:
            await hub.send("@peer", "hello")
        except PeerNotFoundError:
            ...
        except HubTransientError:
            ...
        for msg in await hub.get_unread():
            await hub.ack(msg.id)

Public surface is everything re-exported from this module; everything else
is internal and may change without notice.
"""

from agent_hub_sdk.client import AgentHub
from agent_hub_sdk.config import Config, Mode, resolve_config
from agent_hub_sdk.errors import (
    ConfigurationError,
    HubErrorKind,
    HubTransientError,
    PeerNotFoundError,
    classify_hub_error,
)
from agent_hub_sdk.messages import IncomingMessage, Participant
from agent_hub_sdk.session import HubSession
from agent_hub_sdk.version import __version__

__all__ = [
    "AgentHub",
    "Config",
    "ConfigurationError",
    "HubErrorKind",
    "HubSession",
    "HubTransientError",
    "IncomingMessage",
    "Mode",
    "Participant",
    "PeerNotFoundError",
    "__version__",
    "classify_hub_error",
    "resolve_config",
]
