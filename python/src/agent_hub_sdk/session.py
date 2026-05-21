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

**M2** adds :meth:`HubSession.inbox`, a high-level async iterator that
fuses the SSE push stream, a safety-net poll, a session heartbeat, and an
optional ``/ping`` interceptor. Consumers can now write a single
``async for msg in hub.inbox():`` loop instead of the hand-rolled task
group seen in ``bridge-claude``'s pre-SDK worker.
"""

from __future__ import annotations

import logging
import os
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
    from agent_hub_sdk.commands import CommandRouter

logger = logging.getLogger(__name__)

__all__ = ["HubSession"]

# Inbox push queue capacity. Push events are coalescing hints ("there's
# something to fetch"), not data themselves, so a small bound is fine — if
# the consumer falls behind, dropping a push is harmless because the next
# ``get_unread`` will still see the message. 100 is generous.
_INBOX_QUEUE_CAPACITY = 100

# Default safety-net poll interval for the unified :meth:`HubSession.inbox`
# iterator. Pull-mode fetches every N seconds so that a silently-dead SSE
# stream (= the MCP SDK's GET-stream auto-reconnect gives up after 2 tries
# and stays dead, see bridge-claude/worker.py for the running diagnosis)
# can't pin the bridge in zombie state. Overridable via env so deploys with
# different RTT / load can tune without a code change. Keep in sync with
# ``DEFAULT_INBOX_POLL_INTERVAL_S`` in bridge-claude/worker.py.
_DEFAULT_INBOX_POLL_INTERVAL_S = 30.0
_INBOX_POLL_INTERVAL_ENV = "AGENT_HUB_INBOX_POLL_INTERVAL_S"

# Default heartbeat interval. Long enough that we don't drum on the hub
# uselessly, short enough that a server restart is detected within a minute.
# 60s is the same value bridge-claude / bridge-slack run with today.
_DEFAULT_HEARTBEAT_INTERVAL_S = 60.0

# Yielded-message queue capacity inside :meth:`HubSession.inbox`. Bursts of
# unread fetched from one push/poll cycle all land here before the consumer
# drains them. 1024 is generous — a real bridge processes one message in
# seconds, and we'll get a "queue overflow" warning long before silent loss.
_INBOX_OUTPUT_CAPACITY = 1024

# The protocol-level health-check command. ``hub.inbox(auto_pong=True)``
# (the default) intercepts messages whose body equals this token (after
# trimming whitespace) and replies with :data:`_PONG_REPLY` without
# yielding to the consumer. See issue #10 for motivation: lets operators
# verify a bridge's inbox listener is alive without spending tokens on an
# LLM round-trip.
#
# M2.1 (= issue #13, design issue #10 Rev 2): the reply is plain text
# ``pong``, not the command-format ``/pong``. agent-hub#92 was updated
# in lockstep. Bridges that want the slash-prefix form can override
# ``/ping`` via ``CommandRouter`` (see :mod:`agent_hub_sdk.commands`).
_PING_TOKEN = "/ping"
_PONG_REPLY = "pong"


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
        # ``/status`` built-in handler reads this string. Bridges update
        # it via :meth:`set_status`; default is ``"idle"`` so a bridge
        # that never calls ``set_status`` still answers ``/status``
        # sensibly. See :mod:`agent_hub_sdk.commands` for the protocol.
        self._status: str = "idle"

    def set_status(self, value: str) -> None:
        """Update the value returned by the built-in ``/status`` command.

        Bridges call this from their work loop so an operator hitting
        ``/status`` from the outside sees the current state. Conventional
        values are ``"idle"`` (default), ``"busy"``, ``"rate-limited"``,
        but any non-empty string is allowed — the SDK does not validate.

        Side-effect-free apart from the in-memory write; safe to call
        from any task that has a reference to the session. (The SDK runs
        the inbox loop in one task only, so concurrent writes from
        multiple producer coroutines aren't a real concern in practice;
        the field is a plain attribute, not a lock-guarded structure.)
        """
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"set_status: value must be a non-empty str (got {value!r})"
            )
        self._status = value

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

        This is the low-level pull-based read; for most consumers
        :meth:`inbox` is the right entry point because it fuses push +
        safety-net poll + heartbeat + ``/ping`` interception into a single
        ``async for msg in hub.inbox()`` loop.
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

        .. note::
           **Single-consumer only.** The underlying memory stream
           (``self._notify_recv``) can be iterated by exactly one
           coroutine at a time; a second concurrent ``async for`` over the
           same :class:`HubSession` will compete with the first for events
           and the consumer that loses a given race will block forever or
           skip events. If you need to fan out push events to multiple
           consumers, do it on top of :meth:`inbox` (which already merges
           push + poll + heartbeat into one stream) or build your own
           fan-out queue over a single ``inbox_pushes`` iterator.

           This restriction matches the M1 design (and the original
           ``bridge-claude``/``bridge-slack`` ``HubClient`` shape). Lifted
           in PR #8 review, Suggestion 1, codified here.
        """
        async with self._notify_recv:
            async for uri in self._notify_recv:
                yield uri

    # ------------------------------------------------------------------
    # Inbox iterator (M2) — push + poll + heartbeat + /ping in one stream
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def inbox(
        self,
        *,
        commands: CommandRouter | None = None,
        auto_pong: bool = True,
        poll_interval_s: float | None = None,
        heartbeat_interval_s: float = _DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> AsyncIterator[AsyncIterator[IncomingMessage]]:
        """Async context manager: yields an async iterator over inbox messages.

        This is the SDK's "just listen to my inbox" loop. Three concurrent
        producers feed messages into a single output queue that the caller
        iterates with ``async for``:

        1. **SSE push** — primary low-latency path. ``subscribe_inbox`` is
           called once at startup, then :meth:`inbox_pushes` drives a drain
           every time the server emits a notification.
        2. **Safety-net poll** — defends against the MCP SDK's known
           failure mode where the GET-stream auto-reconnect gives up after
           a couple of tries and the bridge silently stops getting pushes
           (see ``bridge-claude/worker.py`` for the historical diagnosis).
           Polls every ``poll_interval_s`` seconds (default 30s, override
           via the ``AGENT_HUB_INBOX_POLL_INTERVAL_S`` env var).
        3. **Heartbeat** — every ``heartbeat_interval_s`` seconds (default
           60s) calls ``list_tools`` as a liveness probe. If the server
           has invalidated the session the underlying transport raises and
           tears the whole iterator down, which propagates out to the
           caller's reconnect loop.

        Push and poll share a lock so the two never double-process the
        same batch of unread.

        :param commands: optional :class:`~agent_hub_sdk.commands.CommandRouter`.
            Messages whose body starts with ``/`` are routed through it;
            see :mod:`agent_hub_sdk.commands` for the full dispatch
            semantics. When ``None`` (the default), the legacy
            ``auto_pong`` parameter takes effect for backward compat:
            ``auto_pong=True`` (the M2 default) is equivalent to passing
            ``commands=CommandRouter()`` (= ``/ping`` built-in,
            ``/status``, ``/help``, plus ``unknown="reject"``).
            ``auto_pong=False`` is equivalent to ``commands=None`` (= no
            command interception). Passing ``commands=`` explicitly
            **always** wins over ``auto_pong``.
        :param auto_pong: M2 legacy flag — see ``commands``. Kept for
            backward compatibility with bridges that haven't migrated to
            the explicit router yet. Slated for deprecation in a later
            milestone.
        :param poll_interval_s: safety-net poll interval. ``None`` (default)
            uses ``$AGENT_HUB_INBOX_POLL_INTERVAL_S`` if set, otherwise 30s.
        :param heartbeat_interval_s: liveness-probe interval. Default 60s.

        **Lifecycle**: the iterator is a contract between caller and SDK
        for the *current* session only. If the session dies (server
        restart, transport tear-down) the iterator raises; the caller's
        outer loop is responsible for re-entering
        :meth:`agent_hub_sdk.AgentHub.connect` to get a fresh session. M2
        deliberately keeps reconnect at the *caller's* layer to mirror the
        existing bridge pattern; an inside-the-SDK reconnect wrapper is a
        candidate for a later milestone.

        **Why a context manager, not a bare generator?** The three
        producer tasks are owned by an :func:`anyio.create_task_group`
        block, and anyio rejects entering a task group across an
        ``async generator`` ``yield`` boundary (cancel-scope ownership
        ends up on the wrong task). Wrapping the task group in
        :func:`contextlib.asynccontextmanager` keeps scope ownership on
        the original task while still letting the caller use a clean
        ``async for`` over the yielded receive stream.

        Usage::

            async with AgentHub.connect(user="my-bridge") as hub:
                await hub.register()
                async with hub.inbox() as messages:
                    async for msg in messages:
                        await my_handler(msg)
                        await hub.ack(msg.id)
        """
        resolved_poll = (
            poll_interval_s
            if poll_interval_s is not None
            else float(
                os.environ.get(
                    _INBOX_POLL_INTERVAL_ENV, _DEFAULT_INBOX_POLL_INTERVAL_S
                )
            )
        )

        # One lock shared by push + poll so a burst of pushes doesn't race
        # the poll loop into double-processing the same unread batch.
        drain_lock = anyio.Lock()

        # Bounded queue that fans-in messages from push/poll to the consumer.
        # 1024 is generous; overflow logs a warning and drops, which is safe
        # because the underlying messages stay on the server until acked.
        send_stream: MemoryObjectSendStream[IncomingMessage]
        recv_stream: MemoryObjectReceiveStream[IncomingMessage]
        send_stream, recv_stream = anyio.create_memory_object_stream(
            max_buffer_size=_INBOX_OUTPUT_CAPACITY
        )

        # Track message IDs already put in the output queue within this
        # inbox session. Prevents double-yield when an SSE event-store
        # replay triggers a second push before the consumer acks the first
        # delivery (= issue #31 / agent-hub double-dispatch bug).
        #
        # Why this is safe:
        #   - ack'd messages never reappear in get_unread(), so the set only
        #     accumulates IDs for messages currently being processed.
        #   - On queue overflow (_enqueue removes the ID) the message can
        #     re-appear on the next push/poll cycle as intended.
        #   - The set is local to each inbox() call, so a fresh reconnect
        #     starts with an empty set (correct: old in-flight msgs are
        #     still unread on the server and will surface again).
        in_flight_ids: set[str] = set()

        def _enqueue(msg: IncomingMessage, source: str) -> None:
            """Put *msg* in the output queue, deduplicating by message ID.

            Skips silently if the same ID is already in-flight (= enqueued
            but not yet ack'd by the consumer). On queue overflow, removes
            the ID so the message can be re-fetched on the next push/poll.
            """
            if msg.id in in_flight_ids:
                logger.debug(
                    "inbox dedup: skipping already-in-flight %s (source=%s)",
                    msg.id,
                    source,
                )
                return
            in_flight_ids.add(msg.id)
            try:
                send_stream.send_nowait(msg)
            except anyio.WouldBlock:
                # Queue full — remove so the message can re-appear on
                # the next push/poll cycle (stays unread on the server).
                in_flight_ids.discard(msg.id)
                logger.warning(
                    "inbox output queue overflow (%s), dropping", source
                )

        # Resolve which drain function to use. Two paths:
        #
        # - ``commands=router`` (M2.1 new path): every drained message
        #   goes through ``router.dispatch``; handled messages are not
        #   yielded, yielded messages still need a consumer ack.
        # - ``commands=None`` (M2 legacy path): only ``/ping`` is
        #   intercepted, governed by ``auto_pong``. This is the existing
        #   default for back-compat with bridges that haven't migrated
        #   to ``CommandRouter`` yet.
        #
        # Note: the ``/ping`` reply token itself changed from ``/pong``
        # to plain ``pong`` in M2.1 (= agent-hub#92 alignment). Bridges
        # on either path see that textual change.
        if commands is not None:
            async def _drain() -> list[IncomingMessage]:
                return await self._drain_with_router(commands)
        else:
            async def _drain() -> list[IncomingMessage]:
                return await self._drain_intercepting_ping(
                    auto_pong=auto_pong
                )

        # Subscribe before draining so push events emitted *during* the
        # initial drain don't slip through the gap.
        await self.subscribe_inbox()

        # Startup drain: pick up anything queued before subscribe took
        # effect, and ack any /ping that snuck in pre-subscribe.
        for msg in await _drain():
            _enqueue(msg, "startup")

        async def push_loop() -> None:
            """Drain whenever the server emits an inbox push."""
            async for _uri in self.inbox_pushes():
                async with drain_lock:
                    for m in await _drain():
                        _enqueue(m, "push")

        async def poll_loop() -> None:
            """Drain every ``resolved_poll`` seconds as a safety net."""
            while True:
                await anyio.sleep(resolved_poll)
                async with drain_lock:
                    for m in await _drain():
                        _enqueue(m, "poll")

        async def heartbeat_loop() -> None:
            """Periodic liveness probe; raises out if the session is dead."""
            while True:
                await anyio.sleep(heartbeat_interval_s)
                await self.heartbeat()

        async with anyio.create_task_group() as tg:
            tg.start_soon(push_loop)
            tg.start_soon(poll_loop)
            tg.start_soon(heartbeat_loop)
            try:
                async with recv_stream:
                    yield recv_stream
            finally:
                # Consumer left the ``async with`` block (or the session
                # died and one of the inner tasks raised). Cancel the peer
                # tasks so we don't leak running coroutines.
                tg.cancel_scope.cancel()

    async def _drain_with_router(
        self, router: CommandRouter
    ) -> list[IncomingMessage]:
        """Fetch unread, route through ``router``, return what to yield.

        Internal helper for :meth:`inbox` when called with
        ``commands=router``. Mirrors the older
        :meth:`_drain_intercepting_ping` shape but delegates the
        intercept decision to the user-supplied router.

        Handled messages (= dispatch returned ``"handled"``) are already
        replied to + acked by the router. They are **not** in the
        returned list — the inbox iterator should not yield them to the
        consumer.

        Yielded messages (= dispatch returned ``"yield"``) are kept in
        the returned list. The consumer is responsible for acking them
        after processing (same contract as
        :meth:`_drain_intercepting_ping`).
        """
        messages = await self.get_unread()
        yielded: list[IncomingMessage] = []
        for msg in messages:
            try:
                result = await router.dispatch(msg, self)
            except Exception:
                # Router.dispatch is supposed to catch handler errors
                # internally; if something escapes, we still mustn't
                # tear down the inbox iterator. Log and yield so the
                # consumer at least sees the message — but don't ack
                # (= the consumer's job to decide).
                logger.exception(
                    "command router crashed on message %s; yielding to consumer",
                    msg.id,
                )
                yielded.append(msg)
                continue
            if result == "yield":
                yielded.append(msg)
            # result == "handled" → router already acked, drop from yield
        return yielded

    async def _drain_intercepting_ping(
        self, *, auto_pong: bool
    ) -> list[IncomingMessage]:
        """Fetch unread, intercept ``/ping``, return what's left.

        Internal helper for :meth:`inbox`. Pulled out as a method (rather
        than a closure) so it's straightforward to mock in tests and so
        the ``/ping`` handler is a single source of truth.

        When ``auto_pong`` is ``True`` and a message body equals
        :data:`_PING_TOKEN` (post ``str.strip``), this method sends
        :data:`_PONG_REPLY` back to the sender, acks the ping, and drops
        the message from the returned list. ``/pong`` send failures and
        ack failures are logged-and-swallowed — a single misbehaving
        ``/ping`` should not kill the inbox loop.
        """
        messages = await self.get_unread()
        if not auto_pong:
            return messages
        remaining: list[IncomingMessage] = []
        for msg in messages:
            if msg.body.strip() != _PING_TOKEN:
                remaining.append(msg)
                continue
            logger.info("[ping] from=%s → pong", msg.sender)
            try:
                await self.send(msg.sender, _PONG_REPLY)
            except Exception:
                logger.exception(
                    "auto-pong send to %s failed (continuing)", msg.sender
                )
            try:
                await self.ack(msg.id)
            except Exception:
                logger.exception(
                    "auto-pong ack for %s failed (continuing)", msg.id
                )
        return remaining

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
    # One-shot session (M3) — fresh MCP session for one batch of ops
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def one_shot(self) -> AsyncIterator[HubSession]:
        """Open a fresh MCP session for one batch of tool calls.

        Yields a brand-new :class:`HubSession` bound to its own
        independent streamable-HTTP transport + ``ClientSession``. The
        outer session (``self``) is not touched — its long-lived SSE
        subscribe, push queue, and ``_status`` are all preserved.

        Returns to the close-the-connection-after-each-batch shape that
        ``client-litellm`` and other stateless workers want. Typical
        pattern (= M3 stateless mode):

        .. code-block:: python

            async with AgentHub.connect(user="translator", mode="stateless") as hub:
                await hub.register()
                await hub.subscribe_inbox()

                async for _push_uri in hub.inbox_pushes():
                    async with hub.one_shot() as session:
                        for msg in await session.get_unread():
                            reply = await llm.complete(msg.body)
                            await session.send(msg.sender, reply)
                            await session.ack(msg.id)
                    # one_shot block exits → fresh session closed.

        **Mode-agnostic.** Available on any session regardless of the
        ``mode`` declared at :meth:`AgentHub.connect`. Stateful bridges
        can still use ``one_shot()`` for operations they want to isolate
        from their long-lived session (e.g. a particularly transactional
        write).

        **Independence.** The yielded :class:`HubSession`:

        - has its own MCP ``ClientSession`` (= separate ``initialize``,
          separate notification handler, separate push queue);
        - reads from a brand-new push stream (= the outer session's
          push events are not delivered here);
        - shares only the immutable :class:`Config` with the outer hub
          (= same URL, PAT, tenant, user, mode declaration).

        **Cleanup.** When the ``async with`` block exits — normally or
        via exception — the inner session's streamable-HTTP transport
        and ``ClientSession`` are both closed cleanly via the
        underlying :meth:`HubSession.open` context manager.

        **Status carry-over.** ``HubSession._status`` (used by the
        built-in ``/status`` command handler) is not shared. The inner
        session starts at the ``"idle"`` default. If a one-shot handler
        needs to expose ``/status``, set it explicitly inside the block.
        """
        async with HubSession.open(self._config) as fresh:
            yield fresh

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
