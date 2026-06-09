"""High-level :class:`AgentHub` facade.

This is the SDK's main entry point. The 2-line shape (M5):

.. code-block:: python

    async with AgentHub.connect(user="my-bridge", mode="stateful") as hub:
        # `register()` was already called automatically — go straight to work.
        await hub.send("@peer", "hello")

Internally :meth:`AgentHub.connect` resolves config (env + caller args),
opens an MCP session via :class:`HubSession`, **auto-registers** the
consumer with the hub (M5, issue #27), and exposes ``HubSession``'s
methods through the ``hub`` handle. Consumers don't need to think about
the transport layer at all.

Why auto-register: every bridge previously had to remember to call
``hub.register()`` at startup so peers could ``send_message`` to it; the
mode-declaration story (``stateful`` / ``stateless`` / ``global``) gets
the same guarantee. The SDK now does this automatically before yielding
the handle, so the invariant "connect succeeded ⇒ consumer is visible
in ``get_participants``" holds without per-bridge boilerplate.

If the auto-register call fails (network error, server unavailable,
name conflict), :meth:`connect` raises and the underlying MCP session
is torn down before the caller's ``async with`` body runs — same
fail-fast semantics as ``ConfigurationError`` (redline #1).

Subsequent calls to ``hub.register(...)`` remain supported and pass
through to the server, which treats them idempotently (= harmless
duplicate, or an explicit ``display_name`` update if the value
changed).

M1 implements ``mode="stateful"`` end-to-end. ``stateless`` (M3) and
``global`` (= future M-something) modes also get auto-registered with
the declared mode — the SDK does not differentiate at this layer.
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
        client_type: str | None = None,
    ) -> AsyncIterator[HubSession]:
        """Open an agent-hub session and auto-register the consumer.

        Resolves config, opens the MCP session via :class:`HubSession`,
        and calls :meth:`HubSession.register` automatically before
        yielding the handle (M5, issue #27). The ``register`` call
        carries ``user`` as the handle, ``display_name`` (falling back
        to ``user`` when not set), and the declared ``mode`` — so peers
        immediately see this consumer in ``get_participants`` without
        the bridge needing to remember a startup-time ``register()``.

        :param user: SDK consumer's handle, without leading ``@``. Required.
        :param mode: worker-mode declaration. ``stateful`` (default),
            ``stateless`` (M3), or ``global``. All three are auto-registered
            with the declared mode; the SDK does not differentiate at this
            layer.
        :param tenant: tenant scope, or ``None`` for the default tenant.
        :param display_name: human-readable role descriptor; falls back to
            ``user`` if ``None``.
        :param url: agent-hub MCP endpoint. Falls back to ``AGENT_HUB_URL``.
            Missing → :class:`~agent_hub_sdk.errors.ConfigurationError`.
        :param pat: GitHub PAT. Falls back to ``GITHUB_PAT``. Missing →
            :class:`~agent_hub_sdk.errors.ConfigurationError`.
        :param client_type: ``X-Agent-Hub-Client`` header value (e.g.
            ``"agent-hub-bridge/claude"``). Falls back to
            ``AGENT_HUB_CLIENT`` env var. ``None`` → header omitted.
        :raises ConfigurationError: if ``url`` or ``pat`` cannot be
            resolved from args + environment. **No implicit default URL**
            — the SDK refuses to start rather than silently connecting to
            a wrong endpoint.
        :raises Exception: if the auto-register call fails (e.g.
            ``HubTransientError`` on a server hiccup, or a generic
            ``RuntimeError`` for other server-side failures). The MCP
            session is torn down before the exception propagates — the
            caller's ``async with`` body is not entered.

        After connect, calls to ``hub.register(...)`` remain supported
        and pass through to the server, which treats duplicate
        registrations idempotently (= harmless re-confirm, or an
        explicit ``display_name`` update if the field changed).
        """
        config = resolve_config(
            user=user,
            mode=mode,
            tenant=tenant,
            display_name=display_name,
            url=url,
            pat=pat,
            client_type=client_type,
        )
        async with HubSession.open(config) as session:
            # Auto-register before yielding (M5, issue #27). If this
            # raises, the surrounding ``async with HubSession.open``
            # tears the MCP session down on the way out — the caller
            # observes a clean failure, no half-open handle.
            await session.register()
            yield session
