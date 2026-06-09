"""Coverage for the M2 ``hub.inbox()`` unified iterator and ``/ping`` handler.

The iterator merges three concurrent producers (SSE push, safety-net poll,
heartbeat) into a single output stream and optionally intercepts ``/ping``
messages at the SDK layer. These tests use mock MCP sessions and a
controlled push queue to verify each path independently.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp import types

from agent_hub_sdk.config import Config
from agent_hub_sdk.session import HubSession


def _config() -> Config:
    return Config(
        user="me",
        display_name=None,
        tenant=None,
        url="https://hub.example/mcp",
        pat="ghp_xxx",
    )


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _msg(msg_id: str, body: str, sender: str = "@alice") -> dict[str, Any]:
    """Build one row in the ``get_messages`` JSON response shape."""
    return {
        "id": msg_id,
        "from": sender,
        "to": "@me",
        "message": body,
        "timestamp": "2026-05-20T10:00:00Z",
    }


def _make_session(
    *,
    unread_batches: list[list[dict[str, Any]]] | None = None,
) -> tuple[HubSession, AsyncMock, anyio.abc.ObjectSendStream[str]]:
    """Build a :class:`HubSession` wired up for inbox iterator tests.

    Returns ``(session, mcp_mock, push_send)`` where:
      - ``mcp_mock`` is the underlying MCP ``ClientSession`` AsyncMock; its
        ``call_tool`` is set up to return successive ``unread_batches`` for
        ``get_messages`` calls and otherwise default to an empty body.
      - ``push_send`` is the upstream end of the push-notification queue;
        the test pushes a URI to simulate an SSE notification.
    """
    push_send, push_recv = anyio.create_memory_object_stream[str](max_buffer_size=100)
    config = _config()
    mock_mcp = AsyncMock()

    # ``call_tool`` dispatches on the tool name.
    import json

    batches = list(unread_batches or [])

    async def fake_call_tool(name: str, args: dict) -> types.CallToolResult:
        if name == "get_messages":
            payload = batches.pop(0) if batches else []
            return _text_result(json.dumps(payload))
        if name == "send_message":
            return _text_result("ok")
        if name == "mark_as_read":
            return _text_result("")
        return _text_result("")

    mock_mcp.call_tool = AsyncMock(side_effect=fake_call_tool)
    mock_mcp.subscribe_resource = AsyncMock()
    mock_mcp.list_tools = AsyncMock(return_value=object())

    session = HubSession(session=mock_mcp, config=config, notify_recv=push_recv)
    return session, mock_mcp, push_send


class TestDrainInterceptingPing:
    """``_drain_intercepting_ping`` filters /ping out, sends /pong, acks both."""

    async def test_auto_pong_false_passes_everything_through(self) -> None:
        session, _mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "/ping"), _msg("m2", "hello")]]
        )
        result = await session._drain_intercepting_ping(auto_pong=False)
        assert [m.id for m in result] == ["m1", "m2"]

    async def test_ping_is_intercepted_and_replied(self) -> None:
        session, mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "/ping", sender="@operator")]]
        )
        result = await session._drain_intercepting_ping(auto_pong=True)
        # /ping filtered out
        assert result == []
        # /pong sent + ack issued
        calls = [c.args[0] for c in mcp.call_tool.await_args_list]
        assert "get_messages" in calls
        assert "send_message" in calls
        assert "mark_as_read" in calls
        # ``send_message`` payload addresses the /ping sender
        send_call = next(
            c for c in mcp.call_tool.await_args_list
            if c.args[0] == "send_message"
        )
        assert send_call.args[1] == {"to": "@operator", "message": "pong"}
        # ``mark_as_read`` matches the /ping message id
        ack_call = next(
            c for c in mcp.call_tool.await_args_list
            if c.args[0] == "mark_as_read"
        )
        assert ack_call.args[1] == {"message_id": "m1"}

    async def test_whitespace_around_ping_still_matches(self) -> None:
        # exact-match with strip() — leading/trailing whitespace OK.
        session, _mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "  /ping\n")]]
        )
        result = await session._drain_intercepting_ping(auto_pong=True)
        assert result == []

    async def test_non_ping_unchanged(self) -> None:
        session, _mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "hello"), _msg("m2", "/ping suffix")]]
        )
        # "/ping suffix" is NOT exact-match /ping, stays in the batch.
        result = await session._drain_intercepting_ping(auto_pong=True)
        assert [m.id for m in result] == ["m1", "m2"]

    async def test_pong_send_failure_does_not_kill_loop(self) -> None:
        # Simulate /pong send raising → we still ack and continue.
        session, mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "/ping")]]
        )

        async def call_tool_with_send_failure(name: str, args: dict):
            if name == "get_messages":
                import json
                return _text_result(json.dumps([_msg("m1", "/ping")]))
            if name == "send_message":
                raise ConnectionError("temporary send failure")
            if name == "mark_as_read":
                return _text_result("")
            return _text_result("")

        mcp.call_tool = AsyncMock(side_effect=call_tool_with_send_failure)
        result = await session._drain_intercepting_ping(auto_pong=True)
        # Despite send failure, /ping is dropped from the yield list
        # (we still tried; the swallow makes the loop resilient).
        assert result == []


class TestInboxIterator:
    """``hub.inbox()`` merges push + poll + heartbeat into one stream."""

    async def test_yields_startup_drain(self) -> None:
        # Pre-subscribe messages should drain on iterator startup.
        session, _mcp, _push_send = _make_session(
            unread_batches=[[_msg("m1", "hello")]],
        )

        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,  # effectively disable poll for this test
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    break

        with anyio.fail_after(2.0):
            await consume()

        assert received == ["m1"]

    async def test_yields_messages_from_push(self) -> None:
        # First call (startup drain) → empty; push triggers a 2nd call that
        # returns one unread.
        session, _mcp, push_send = _make_session(
            unread_batches=[[], [_msg("m1", "from push")]],
        )
        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    break

        async def fire_push() -> None:
            await anyio.sleep(0.05)
            push_send.send_nowait("inbox://@me")

        with anyio.fail_after(2.0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(consume)
                tg.start_soon(fire_push)

        assert received == ["m1"]

    async def test_yields_messages_from_poll(self) -> None:
        # Startup drain empty; poll picks up the message on its next tick.
        session, _mcp, _push = _make_session(
            unread_batches=[[], [_msg("m1", "from poll")]],
        )
        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=0.05,  # tight poll for fast test
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    break

        with anyio.fail_after(2.0):
            await consume()

        assert received == ["m1"]

    async def test_auto_pong_default_intercepts_ping_in_iterator(
        self,
    ) -> None:
        # Startup: a /ping and a real message. Default auto_pong=True →
        # /ping never appears in the iterator.
        session, mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "/ping"), _msg("m2", "hello")]],
        )
        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    break

        with anyio.fail_after(2.0):
            await consume()

        assert received == ["m2"]
        # /pong was sent
        send_calls = [
            c for c in mcp.call_tool.await_args_list
            if c.args[0] == "send_message"
        ]
        assert any(c.args[1] == {"to": "@alice", "message": "pong"} for c in send_calls)

    async def test_auto_pong_false_yields_ping_to_consumer(self) -> None:
        # opt-out: caller wants to see /ping themselves.
        session, mcp, _push = _make_session(
            unread_batches=[[_msg("m1", "/ping")]],
        )
        received: list[tuple[str, str]] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append((msg.id, msg.body))
                    break

        with anyio.fail_after(2.0):
            await consume()

        assert received == [("m1", "/ping")]
        # No /pong sent — the SDK got out of the way.
        send_calls = [
            c for c in mcp.call_tool.await_args_list
            if c.args[0] == "send_message"
        ]
        assert send_calls == []

    async def test_heartbeat_runs_periodically(self) -> None:
        # With tight heartbeat interval, list_tools is called at least once
        # within the window of the test.
        session, mcp, _push_send = _make_session(
            unread_batches=[[_msg("m1", "hello")]],
        )

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,
                heartbeat_interval_s=0.05,
            ) as messages:
                async for msg in messages:
                    _ = msg
                    # Wait long enough for a heartbeat to fire, then break.
                    await anyio.sleep(0.15)
                    break

        with anyio.fail_after(2.0):
            await consume()

        # list_tools (heartbeat) was called at least once.
        assert mcp.list_tools.await_count >= 1

    async def test_session_dead_propagates_out(self) -> None:
        # Heartbeat raising → iterator surfaces the exception to the
        # caller's outer loop (= the reconnect contract).
        session, mcp, _push = _make_session(unread_batches=[[]])
        mcp.list_tools = AsyncMock(side_effect=ConnectionError("session lost"))

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,
                heartbeat_interval_s=0.01,
            ) as messages:
                async for _msg in messages:
                    pass  # pragma: no cover — no messages flow in this scenario

        with pytest.raises((ConnectionError, BaseExceptionGroup)):
            with anyio.fail_after(2.0):
                await consume()


class TestInboxDedup:
    """``hub.inbox()`` deduplicates messages by ID within a session (issue #31)."""

    async def test_sse_replay_does_not_double_yield(self) -> None:
        """Second push while a message is in-flight must not re-yield the same ID.

        Reproduces the double-dispatch bug: SSE event-store replay triggers a
        second push before the consumer acks. ``get_messages`` still returns the
        same unread message because ack hasn't happened yet. The dedup in
        ``_enqueue`` must skip the second occurrence.
        """
        import json

        # get_messages returns m1 on both the 1st and 2nd drain call
        # (simulating "not yet acked" between the two pushes).
        m1 = _msg("m1", "hello")
        drain_calls = 0

        push_send, push_recv = anyio.create_memory_object_stream[str](
            max_buffer_size=10
        )
        config = _config()
        mock_mcp = AsyncMock()

        async def fake_call_tool(name: str, args: dict) -> types.CallToolResult:
            nonlocal drain_calls
            if name == "get_messages":
                drain_calls += 1
                # Both drain calls return the same unread message
                return _text_result(json.dumps([m1]))
            if name in ("mark_as_read", "send_message"):
                return _text_result("")
            return _text_result("")

        mock_mcp.call_tool = AsyncMock(side_effect=fake_call_tool)
        mock_mcp.subscribe_resource = AsyncMock()
        mock_mcp.list_tools = AsyncMock(return_value=object())

        session = HubSession(session=mock_mcp, config=config, notify_recv=push_recv)
        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    # Simulate slow processing (ack not yet called)
                    await anyio.sleep(0.05)
                    # Fire second push while "processing" — same msg still unread
                    push_send.send_nowait("inbox://@me")
                    await anyio.sleep(0.05)
                    break  # Consumer done; don't ack (to keep m1 "unread")

        # Fire the first push shortly after inbox starts
        async def fire_first_push() -> None:
            await anyio.sleep(0.02)
            push_send.send_nowait("inbox://@me")

        with anyio.fail_after(3.0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(consume)
                tg.start_soon(fire_first_push)

        # Verify that get_messages was actually called at least twice (= the
        # second push did fire a second drain, proving the dedup was exercised).
        assert drain_calls >= 2, (
            f"expected at least 2 drain calls to prove dedup was exercised, "
            f"got {drain_calls}"
        )
        # m1 must appear exactly once despite two pushes returning it
        assert received == ["m1"], f"expected ['m1'], got {received}"

    async def test_different_ids_both_yielded(self) -> None:
        """Two distinct message IDs from successive drains must both be yielded."""
        import json

        m1 = _msg("m1", "first")
        m2 = _msg("m2", "second")

        push_send, push_recv = anyio.create_memory_object_stream[str](
            max_buffer_size=10
        )
        config = _config()
        mock_mcp = AsyncMock()
        batches = [[m1], [m2]]

        async def fake_call_tool(name: str, args: dict) -> types.CallToolResult:
            if name == "get_messages":
                payload = batches.pop(0) if batches else []
                return _text_result(json.dumps(payload))
            return _text_result("")

        mock_mcp.call_tool = AsyncMock(side_effect=fake_call_tool)
        mock_mcp.subscribe_resource = AsyncMock()
        mock_mcp.list_tools = AsyncMock(return_value=object())

        session = HubSession(session=mock_mcp, config=config, notify_recv=push_recv)
        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    if len(received) >= 2:
                        break

        async def fire_pushes() -> None:
            await anyio.sleep(0.02)
            try:
                push_send.send_nowait("inbox://@me")
            except anyio.BrokenResourceError:
                return
            await anyio.sleep(0.02)
            try:
                push_send.send_nowait("inbox://@me")
            except anyio.BrokenResourceError:
                return

        with anyio.fail_after(3.0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(consume)
                tg.start_soon(fire_pushes)

        assert received == ["m1", "m2"]


class TestInboxPollIntervalEnv:
    """``AGENT_HUB_INBOX_POLL_INTERVAL_S`` overrides the default poll."""

    async def test_env_override_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Tight env-override → poll fires fast even without a kwarg.
        monkeypatch.setenv("AGENT_HUB_INBOX_POLL_INTERVAL_S", "0.05")
        session, _mcp, _push = _make_session(
            unread_batches=[[], [_msg("m1", "from env-tuned poll")]],
        )
        received: list[str] = []

        async def consume() -> None:
            async with session.inbox(
                auto_pong=False,
                heartbeat_interval_s=3600.0,
                # poll_interval_s left as None → env-driven
            ) as messages:
                async for msg in messages:
                    received.append(msg.id)
                    break

        with anyio.fail_after(2.0):
            await consume()

        assert received == ["m1"]
