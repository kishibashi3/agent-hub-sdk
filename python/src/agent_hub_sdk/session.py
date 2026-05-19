"""MCP session lifecycle for the ``stateful`` mode.

``HubSession`` owns one MCP ``ClientSession`` over streamable HTTP, plus a
queue of inbox-push URIs fed by the session's notification handler. It is
the layer that talks the MCP protocol; :class:`agent_hub_sdk.client.AgentHub`
wraps it with the ergonomic public API.

This module deliberately exposes the same surface as the existing
``bridge-slack``/``bridge-claude`` ``HubClient``: ``register``, ``send``,
``send_with_retry``, ``get_unread``, ``ack``, ``get_participants``,
``subscribe_inbox``, ``inbox_pushes``, ``heartbeat``. Consumers migrating
off those bridges should find a 1:1 mapping.

The high-level inbox iterator that fuses ``subscribe_inbox`` + safety-net
polling + reconnect (described in ``docs/design.md`` §4) lands in **M2**;
M1 just exposes the building blocks.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

from agent_hub_sdk.config import Config, make_headers
from agent_hub_sdk.errors import HubTransientError
from agent_hub_sdk.messages import (
    IncomingMessage,
    Participant,
    parse_messages,
    parse_participants,
)
from agent_hub_sdk.transport import (
    raise_for_send_error,
    raise_for_tool_error,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = ["HubSession"]

# Inbox push queue capacity. Push events are coalescing hints ("there's
# something to fetch"), not data themselves, so a small bound is fine — if
# the consumer falls behind, dropping a push is harmless because the next
# ``get_unread`` will still see the message. 100 is generous.
_INBOX_QUEUE_CAPACITY = 100


class HubSession:
    """One MCP session against agent-hub, plus an inbox-push queue.

    Built by :meth:`HubSession.open` as an async context manager. Don't
    construct directly — :func:`agent_hub_sdk.AgentHub.connect` wraps this.
    """

    def __init__(
        self,
        session: ClientSession,
        config: Config,
        notify_recv: MemoryObjectReceiveStream[str],
    ) -> None:
        self._session = session
        self._config = config
        self._notify_recv = notify_recv

    @classmethod
    @asynccontextmanager
    async def open(cls, config: Config) -> AsyncIterator[HubSession]:
        """Open the MCP session and yield a :class:`HubSession`.

        The inbox-push notification handler is wired up before the session
        is initialised so a ``ResourceUpdatedNotification`` arriving during
        startup is not lost.

        The session is closed (and the push queue drained) when the
        ``async with`` block exits, including on exception.
        """
        send_stream: MemoryObjectSendStream[str]
        recv_stream: MemoryObjectReceiveStream[str]
        send_stream, recv_stream = anyio.create_memory_object_stream(
            max_buffer_size=_INBOX_QUEUE_CAPACITY
        )

        async def handler(message: object) -> None:
            if isinstance(message, types.ServerNotification):
                root = message.root
                if isinstance(root, types.ResourceUpdatedNotification):
                    try:
                        send_stream.send_nowait(str(root.params.uri))
                    except anyio.WouldBlock:
                        # Consumer is behind — dropping a push is harmless
                        # because pulls (get_unread) will still see the data.
                        logger.warning("inbox push queue overflow, dropping")

        async with streamablehttp_client(
            url=config.url,
            headers=make_headers(config),
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write, message_handler=handler) as session:
                await session.initialize()
                hub = cls(session=session, config=config, notify_recv=recv_stream)
                try:
                    yield hub
                finally:
                    await send_stream.aclose()

    # ------------------------------------------------------------------
    # Registration / presence
    # ------------------------------------------------------------------

    async def register(self) -> str:
        """Register the SDK consumer with the hub.

        Sends the ``register`` tool with the configured ``user``,
        ``display_name``, and ``mode``. Returns the server's confirmation
        text (useful for logging the registered display name back).
        """
        display_name = self._config.display_name or self._config.user
        result = await self._session.call_tool(
            "register",
            {
                "name": self._config.user,
                "display_name": display_name,
                "mode": self._config.mode,
            },
        )
        return raise_for_tool_error(result, op="register")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, to: str, message: str) -> str:
        """Send a message to a peer or team.

        :param to: recipient handle (``@peer`` or ``@team``).
        :param message: body text.
        :raises PeerNotFoundError: recipient not registered / offline.
        :raises HubTransientError: 5xx / network / timeout.
        :raises RuntimeError: any other error class the server returns.
        """
        try:
            result = await self._session.call_tool(
                "send_message",
                {"to": to, "message": message},
            )
        except Exception as exc:
            raise HubTransientError(
                f"send to {to} transport failure: {exc}"
            ) from exc

        return raise_for_send_error(result, to=to)

    async def send_with_retry(
        self,
        to: str,
        message: str,
        *,
        max_attempts: int = 3,
        base_delay_s: float = 1.0,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> str:
        """Retry :meth:`send` on transient errors with exponential backoff.

        Default schedule: 3 attempts with 1s, 2s backoff between them (i.e.
        roughly 3 seconds of total wait worst case). The cap is deliberately
        small so a user-facing bridge (e.g. Slack) doesn't keep the
        end-user waiting; the consumer is expected to surface a "hub
        temporarily unavailable" notice when the retry budget is exhausted.

        :param max_attempts: total attempts (≥ 1).
        :param base_delay_s: first sleep; subsequent sleeps double each time.
        :param sleep_fn: injected sleep for tests; defaults to
            :func:`anyio.sleep`.

        :raises PeerNotFoundError: raised immediately (retry is meaningless).
        :raises HubTransientError: the final attempt was still transient.
        :raises RuntimeError: any non-retriable failure.
        """
        sleep = sleep_fn if sleep_fn is not None else anyio.sleep
        last_transient: HubTransientError | None = None
        for attempt in range(max_attempts):
            try:
                return await self.send(to, message)
            except HubTransientError as exc:
                last_transient = exc
                if attempt >= max_attempts - 1:
                    raise
                delay = base_delay_s * (2 ** attempt)
                logger.warning(
                    "send to %s transient (attempt %d/%d), sleeping %.1fs",
                    to,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                await sleep(delay)
        # Unreachable — the loop always either returns or raises.
        assert last_transient is not None
        raise last_transient  # pragma: no cover

    # ------------------------------------------------------------------
    # Inbox: read / ack / subscribe / pushes
    # ------------------------------------------------------------------

    async def get_unread(self) -> list[IncomingMessage]:
        """Fetch unread messages for the SDK consumer.

        This is the synchronous (pull-based) read; M2 will add a unified
        async iterator that fuses push + safety-net poll + reconnect into a
        single ``async for msg in hub.inbox()`` loop.
        """
        result = await self._session.call_tool("get_messages", {})
        text = raise_for_tool_error(result, op="get_messages")
        return parse_messages(text)

    async def ack(self, message_id: str) -> None:
        """Mark a message as read on the server.

        Idempotent on the server side; safe to call twice if the consumer
        loses track.
        """
        result = await self._session.call_tool(
            "mark_as_read", {"message_id": message_id}
        )
        # Ignore the returned text — confirmation only.
        raise_for_tool_error(result, op="mark_as_read")

    async def subscribe_inbox(self) -> None:
        """Enable SSE push notifications for ``inbox://@<self>``.

        After this call returns, :meth:`inbox_pushes` will start yielding
        URIs as the server emits ``ResourceUpdatedNotification`` events.
        """
        inbox_uri = AnyUrl(f"inbox://@{self._config.user}")
        await self._session.subscribe_resource(inbox_uri)

    async def inbox_pushes(self) -> AsyncIterator[str]:
        """Yield inbox-push URIs as the server emits them.

        Each yield is a coalescing hint — the consumer should call
        :meth:`get_unread` to fetch the actual messages, since multiple
        pushes can collapse into one fetch when traffic is bursty.

        Stops iterating when the session is closed.
        """
        async with self._notify_recv:
            async for uri in self._notify_recv:
                yield uri

    # ------------------------------------------------------------------
    # Other tools
    # ------------------------------------------------------------------

    async def get_participants(self) -> list[Participant]:
        """List registered ``person``-type participants.

        Teams are filtered out at parse time (see :mod:`messages`); team
        surface is deferred to a later milestone.
        """
        try:
            result = await self._session.call_tool("get_participants", {})
        except Exception as exc:
            raise HubTransientError(
                f"get_participants transport failure: {exc}"
            ) from exc
        text = raise_for_tool_error(result, op="get_participants")
        return parse_participants(text)

    async def heartbeat(self) -> None:
        """Cheap keepalive that doubles as a session-liveness probe.

        Calls ``list_tools`` (read-only, no side effects). If the underlying
        MCP session has been invalidated by the server (e.g. after a restart),
        the streamable-HTTP transport tears down its task group and the
        owning ``async with`` block raises an :class:`ExceptionGroup` that
        the consumer's reconnect loop can catch.
        """
        # Ignored result — we only care that the call doesn't raise.
        _ = await self._session.list_tools()

    # ------------------------------------------------------------------
    # Raw escape hatch (deliberately unstable)
    # ------------------------------------------------------------------

    async def _call_tool_raw(
        self, name: str, arguments: dict[str, object]
    ) -> str:
        """Call an arbitrary MCP tool and return its text payload.

        Reserved for migration scenarios where a consumer needs a tool the
        SDK hasn't wrapped yet; will probably be removed once the SDK
        catches up. Not part of the stable surface — do not rely on this
        across versions.
        """
        result = await self._session.call_tool(name, arguments)
        return raise_for_tool_error(result, op=name)
