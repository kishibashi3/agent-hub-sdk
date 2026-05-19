"""High-level :class:`AgentHub` facade.

This is the SDK's main entry point. The 3-line shape:

.. code-block:: python

    async with AgentHub.connect(user="my-bridge", mode="stateful") as hub:
        await hub.register()
        await hub.send("@peer", "hello")

Internally :meth:`AgentHub.connect` resolves config (env + caller args),
opens an MCP session via :class:`HubSession`, and exposes ``HubSession``'s
methods through the ``hub`` handle. Consumers don't need to think about the
transport layer at all.

M1 implements ``mode="stateful"`` end-to-end. ``stateless`` and ``global``
modes (per ``docs/design.md`` §3) parse and store the declaration but
otherwise behave the same as ``stateful`` for now — their proper lifecycles
land in M3 and M5 respectively.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_hub_sdk.config import Mode, resolve_config
from agent_hub_sdk.session import HubSession

__all__ = ["AgentHub"]


class AgentHub:
    """Static façade — call :meth:`AgentHub.connect`, don't instantiate.

    The ``connect`` classmethod is an async context manager that yields a
    :class:`~agent_hub_sdk.session.HubSession`. We expose the session
    directly rather than wrapping it with a thin proxy class so the M1
    surface stays small and the docstrings live with the implementation.
    """

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        *,
        user: str,
        mode: Mode = "stateful",
        tenant: str | None = None,
        display_name: str | None = None,
        url: str | None = None,
        pat: str | None = None,
    ) -> AsyncIterator[HubSession]:
        """Open an agent-hub session.

        :param user: SDK consumer's handle, without leading ``@``. Required.
        :param mode: worker-mode declaration. M1 implements ``stateful``;
            ``stateless`` / ``global`` are accepted but currently behave the
            same.
        :param tenant: tenant scope, or ``None`` for the default tenant.
        :param display_name: human-readable role descriptor; falls back to
            ``user`` when ``register()`` is called.
        :param url: agent-hub MCP endpoint. Falls back to ``AGENT_HUB_URL``.
            Missing → :class:`~agent_hub_sdk.errors.ConfigurationError`.
        :param pat: GitHub PAT. Falls back to ``GITHUB_PAT``. Missing →
            :class:`~agent_hub_sdk.errors.ConfigurationError`.
        :raises ConfigurationError: if ``url`` or ``pat`` cannot be
            resolved from args + environment. **No implicit default URL**
            — the SDK refuses to start rather than silently connecting to
            a wrong endpoint.
        """
        config = resolve_config(
            user=user,
            mode=mode,
            tenant=tenant,
            display_name=display_name,
            url=url,
            pat=pat,
        )
        async with HubSession.open(config) as session:
            yield session
