"""agent-hub-sdk — Unified Python SDK for agent-hub.

Status: M1 (Python core, stateful mode). See ``docs/design.md`` for the full
roadmap and ``CHANGELOG.md`` for what's landed.

Typical usage::

    from agent_hub_sdk import AgentHub, ParticipantNotFoundError, HubTransientError

    async with AgentHub.connect(participant="my-bridge") as hub:
        await hub.register()
        try:
            await hub.send("@peer", "hello")
        except ParticipantNotFoundError:
            ...
        except HubTransientError:
            ...
        for msg in await hub.get_unread():
            await hub.ack(msg.id)

Public surface is everything re-exported from this module; everything else
is internal and may change without notice.
"""

from agent_hub_sdk.client import AgentHub
from agent_hub_sdk.commands import (
    CommandHandler,
    CommandRouter,
    DispatchResult,
    RestartHandler,
    parse_command,
)
from agent_hub_sdk.config import Config, resolve_config
from agent_hub_sdk.errors import (
    ConfigurationError,
    HubErrorKind,
    HubTransientError,
    ParticipantNotFoundError,
    PeerNotFoundError,
    classify_hub_error,
)
from agent_hub_sdk.messages import IncomingMessage, Participant
from agent_hub_sdk.session import HubSession
from agent_hub_sdk.version import __version__

__all__ = [
    "AgentHub",
    "CommandHandler",
    "CommandRouter",
    "Config",
    "ConfigurationError",
    "DispatchResult",
    "HubErrorKind",
    "HubSession",
    "HubTransientError",
    "IncomingMessage",
    "Participant",
    "ParticipantNotFoundError",
    "PeerNotFoundError",
    "RestartHandler",
    "__version__",
    "classify_hub_error",
    "parse_command",
    "resolve_config",
]
