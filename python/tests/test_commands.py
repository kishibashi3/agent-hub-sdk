"""Coverage for the M2.1 ``CommandRouter`` and the built-in handlers.

Tests build a :class:`HubSession` with a mocked MCP session (same shape
as the M2 inbox tests) and drive ``router.dispatch`` directly. The
``hub.inbox(commands=router)`` integration is exercised separately in
:mod:`tests.test_inbox_iterator`'s router-mode additions.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp import types

from agent_hub_sdk import CommandRouter, IncomingMessage, parse_command
from agent_hub_sdk.config import Config
from agent_hub_sdk.session import HubSession

# ---------------------------------------------------------------------
# parse_command
# ---------------------------------------------------------------------


class TestParseCommand:
    """Pure function — no fixtures needed."""

    def test_basic_command(self) -> None:
        assert parse_command("/list") == ("/list", "")

    def test_command_with_args(self) -> None:
        assert parse_command("/list verbose") == ("/list", "verbose")

    def test_command_with_multi_token_args(self) -> None:
        # Args is everything-after-first-space, trimmed.
        assert parse_command("/add foo 0 * * *") == ("/add", "foo 0 * * *")

    def test_whitespace_around_command(self) -> None:
        # Leading/trailing whitespace is stripped before parsing.
        assert parse_command("  /ping  ") == ("/ping", "")

    def test_non_command_body(self) -> None:
        assert parse_command("hello world") == (None, "")

    def test_bare_slash_is_not_a_command(self) -> None:
        # A lone ``/`` is not a command (= no command token to match).
        assert parse_command("/") == (None, "")

    def test_empty_and_none(self) -> None:
        assert parse_command("") == (None, "")
        assert parse_command(None) == (None, "")


# ---------------------------------------------------------------------
# Fixtures shared by router / dispatch tests
# ---------------------------------------------------------------------


def _config() -> Config:
    return Config(
        user="me",
        display_name=None,
        mode="stateful",
        tenant=None,
        url="https://hub.example/mcp",
        pat="ghp_xxx",
    )


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _msg(
    msg_id: str = "m1",
    body: str = "/ping",
    sender: str = "@alice",
) -> IncomingMessage:
    return IncomingMessage(
        id=msg_id,
        sender=sender,
        to="@me",
        body=body,
        timestamp="2026-05-20T10:00:00Z",
    )


def _session(call_tool_side_effect=None) -> tuple[HubSession, AsyncMock]:
    """Build a HubSession with a mocked MCP. Returns ``(session, mcp)``."""
    _send, recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
    mcp = AsyncMock()
    if call_tool_side_effect is None:
        mcp.call_tool = AsyncMock(return_value=_text_result("ok"))
    else:
        mcp.call_tool = AsyncMock(side_effect=call_tool_side_effect)
    return HubSession(session=mcp, config=_config(), notify_recv=recv), mcp


def _send_calls(mcp: AsyncMock) -> list[dict[str, Any]]:
    """Filter ``mcp.call_tool`` history to the ``send_message`` calls."""
    return [
        c.args[1]
        for c in mcp.call_tool.await_args_list
        if c.args[0] == "send_message"
    ]


def _ack_calls(mcp: AsyncMock) -> list[dict[str, Any]]:
    return [
        c.args[1]
        for c in mcp.call_tool.await_args_list
        if c.args[0] == "mark_as_read"
    ]


# ---------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------


class TestRegistration:
    def test_command_path_must_start_with_slash(self) -> None:
        router = CommandRouter()
        with pytest.raises(ValueError, match="must start with"):

            @router.command("list")
            async def _h(msg, hub, args):  # pragma: no cover
                return None

    def test_passthrough_path_must_start_with_slash(self) -> None:
        router = CommandRouter()
        with pytest.raises(ValueError, match="must start with"):
            router.passthrough("help")

    def test_re_register_overrides_handler(self) -> None:
        router = CommandRouter()

        @router.command("/active")
        async def h1(msg, hub, args):  # pragma: no cover
            return "v1"

        @router.command("/active")
        async def h2(msg, hub, args):  # pragma: no cover
            return "v2"

        assert router._handlers["/active"] is h2

    def test_passthrough_idempotent(self) -> None:
        router = CommandRouter()
        router.passthrough("/explain")
        router.passthrough("/explain")
        assert "/explain" in router._passthrough


# ---------------------------------------------------------------------
# Dispatch -- 4 states x yield/reject
# ---------------------------------------------------------------------


class TestDispatchNonCommand:
    async def test_non_command_yields(self) -> None:
        session, _mcp = _session()
        router = CommandRouter()
        result = await router.dispatch(_msg(body="hello world"), session)
        assert result == "yield"

    async def test_empty_body_yields(self) -> None:
        session, _mcp = _session()
        router = CommandRouter()
        result = await router.dispatch(_msg(body=""), session)
        assert result == "yield"


class TestDispatchUserHandler:
    async def test_handler_str_return_sends_reply_and_acks(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/active", description="current task")
        async def handle_active(msg, hub, args):
            return "working on M2.1"

        result = await router.dispatch(_msg(body="/active"), session)
        assert result == "handled"
        assert _send_calls(mcp) == [{"to": "@alice", "message": "working on M2.1"}]
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_handler_none_return_no_reply_but_acks(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/silent")
        async def handle_silent(msg, hub, args):
            return None  # explicit no-reply

        result = await router.dispatch(_msg(body="/silent"), session)
        assert result == "handled"
        assert _send_calls(mcp) == []  # no auto-reply
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_handler_receives_args(self) -> None:
        session, _mcp = _session()
        router = CommandRouter()
        seen: list[str] = []

        @router.command("/list")
        async def handle_list(msg, hub, args):
            seen.append(args)
            return None

        await router.dispatch(_msg(body="/list verbose pending"), session)
        assert seen == ["verbose pending"]

    async def test_handler_exception_does_not_propagate(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/buggy")
        async def handle_buggy(msg, hub, args):
            raise ValueError("intentional test failure")

        result = await router.dispatch(_msg(body="/buggy"), session)
        assert result == "handled"
        # Generic warning reply sent to caller
        replies = _send_calls(mcp)
        assert len(replies) == 1
        assert replies[0]["to"] == "@alice"
        assert "/buggy failed" in replies[0]["message"]
        # Acked regardless
        assert _ack_calls(mcp) == [{"message_id": "m1"}]


class TestDispatchBuiltinPing:
    async def test_ping_replies_plain_pong(self) -> None:
        session, mcp = _session()
        router = CommandRouter()
        result = await router.dispatch(_msg(body="/ping"), session)
        assert result == "handled"
        # ⭐ M2.1 Rev 2: plain text, not "/pong"
        assert _send_calls(mcp) == [{"to": "@alice", "message": "pong"}]
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_ping_whitespace_tolerant(self) -> None:
        session, mcp = _session()
        router = CommandRouter()
        await router.dispatch(_msg(body="  /ping\n"), session)
        assert _send_calls(mcp) == [{"to": "@alice", "message": "pong"}]

    async def test_user_handler_overrides_builtin_ping(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/ping")
        async def custom_ping(msg, hub, args):
            return "/pong"  # slash form for legacy peers

        result = await router.dispatch(_msg(body="/ping"), session)
        assert result == "handled"
        assert _send_calls(mcp) == [{"to": "@alice", "message": "/pong"}]

    async def test_builtins_disabled_yields_ping(self) -> None:
        session, mcp = _session()
        router = CommandRouter(builtins=False, unknown="yield")
        result = await router.dispatch(_msg(body="/ping"), session)
        assert result == "yield"
        assert _send_calls(mcp) == []


class TestDispatchBuiltinStatus:
    async def test_status_returns_default_idle(self) -> None:
        session, mcp = _session()
        router = CommandRouter()
        result = await router.dispatch(_msg(body="/status"), session)
        assert result == "handled"
        assert _send_calls(mcp) == [{"to": "@alice", "message": "idle"}]

    async def test_status_reflects_set_status(self) -> None:
        session, mcp = _session()
        session.set_status("busy")
        router = CommandRouter()
        await router.dispatch(_msg(body="/status"), session)
        assert _send_calls(mcp) == [{"to": "@alice", "message": "busy"}]

    async def test_set_status_rejects_empty(self) -> None:
        session, _mcp = _session()
        with pytest.raises(ValueError, match="non-empty"):
            session.set_status("")


class TestDispatchBuiltinHelp:
    async def test_help_lists_builtins_and_user_commands(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/list", description="list jobs")
        async def handle_list(msg, hub, args):  # pragma: no cover
            return None

        @router.command("/add", description="add a job")
        async def handle_add(msg, hub, args):  # pragma: no cover
            return None

        router.passthrough("/explain", description="ask the LLM")

        await router.dispatch(_msg(body="/help"), session)
        assert len(_send_calls(mcp)) == 1
        body = _send_calls(mcp)[0]["message"]
        # Built-ins (in declared order)
        assert "/ping" in body
        assert "/status" in body
        assert "/help" in body
        # User-registered (with descriptions)
        assert "/list" in body and "list jobs" in body
        assert "/add" in body and "add a job" in body
        # Passthrough
        assert "/explain" in body and "ask the LLM" in body

    async def test_help_user_override_replaces_builtin(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/help", description="our custom help")
        async def custom_help(msg, hub, args):
            return "custom help body"

        await router.dispatch(_msg(body="/help"), session)
        # Override sent its body, not the auto-generated list
        assert _send_calls(mcp) == [
            {"to": "@alice", "message": "custom help body"}
        ]

    async def test_help_with_builtins_disabled_and_no_handlers(self) -> None:
        session, mcp = _session()
        router = CommandRouter(builtins=False, unknown="yield")
        # No handlers registered; /help itself is now an unknown command
        # (because builtins disabled). Verify it yields rather than crash.
        result = await router.dispatch(_msg(body="/help"), session)
        assert result == "yield"
        assert _send_calls(mcp) == []

    async def test_help_lists_restart_builtin(self) -> None:
        # M6 (issue #26): /restart is a built-in, so /help lists it
        # regardless of whether the bridge has registered a restart
        # handler — the listing advertises the *protocol*, not bridge
        # readiness.
        session, mcp = _session()
        router = CommandRouter()
        await router.dispatch(_msg(body="/help"), session)
        body = _send_calls(mcp)[0]["message"]
        assert "/restart" in body
        assert "reset the bridge's session context" in body


class TestDispatchBuiltinRestart:
    """M6 (issue #26): ``/restart`` built-in dispatch behaviour.

    Two-stage protocol:
      - No handler registered → ack-only, no send.
      - Handler registered → send "restarting...", await handler,
        send "ready" on success / warning on exception, then ack.

    User-registered ``/restart`` handler via :meth:`CommandRouter.command`
    overrides the built-in (= same precedence rule as /ping /status /help).
    ``builtins=False`` disables /restart along with the other built-ins.
    """

    async def test_no_handler_registered_acks_only(self) -> None:
        session, mcp = _session()
        router = CommandRouter()
        # No set_restart_handler call — bridge declared no restart action.
        result = await router.dispatch(_msg(body="/restart"), session)
        assert result == "handled"
        # Stateless / no-restart semantic: no send, just ack.
        assert _send_calls(mcp) == []
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_handler_two_stage_send_and_ack(self) -> None:
        session, mcp = _session()
        router = CommandRouter()
        handler_called = False

        async def my_restart() -> None:
            nonlocal handler_called
            handler_called = True
            # By the time the handler runs, the accept reply must
            # already have been sent — that's the documented protocol.
            sends = _send_calls(mcp)
            assert len(sends) == 1
            assert sends[0] == {"to": "@alice", "message": "restarting..."}

        router.set_restart_handler(my_restart)

        result = await router.dispatch(_msg(body="/restart"), session)
        assert result == "handled"
        assert handler_called is True
        assert _send_calls(mcp) == [
            {"to": "@alice", "message": "restarting..."},
            {"to": "@alice", "message": "ready"},
        ]
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_handler_exception_warns_no_ready_still_acks(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        async def failing_restart() -> None:
            raise RuntimeError("respawn failed")

        router.set_restart_handler(failing_restart)

        result = await router.dispatch(_msg(body="/restart"), session)
        assert result == "handled"
        sends = _send_calls(mcp)
        # 1) "restarting...", 2) warning. No "ready".
        assert len(sends) == 2
        assert sends[0] == {"to": "@alice", "message": "restarting..."}
        assert "/restart failed" in sends[1]["message"]
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_user_handler_overrides_builtin(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        @router.command("/restart")
        async def custom_restart(msg, hub, args):
            return "custom-restart-reply"

        # Even with set_restart_handler ALSO registered, the user
        # handler from @router.command takes precedence — same
        # precedence rule as /ping /status /help overrides.
        async def should_not_run() -> None:
            raise AssertionError("user handler should win")

        router.set_restart_handler(should_not_run)

        result = await router.dispatch(_msg(body="/restart"), session)
        assert result == "handled"
        assert _send_calls(mcp) == [
            {"to": "@alice", "message": "custom-restart-reply"}
        ]
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_builtins_false_yields_restart(self) -> None:
        session, mcp = _session()
        router = CommandRouter(builtins=False, unknown="yield")
        # Even with a restart handler registered, builtins=False skips
        # the built-in dispatch path — shared semantic with /ping etc.

        async def should_not_run() -> None:
            raise AssertionError("should not run when builtins=False")

        router.set_restart_handler(should_not_run)

        result = await router.dispatch(_msg(body="/restart"), session)
        assert result == "yield"
        assert _send_calls(mcp) == []
        assert _ack_calls(mcp) == []

    async def test_set_restart_handler_none_clears(self) -> None:
        session, mcp = _session()
        router = CommandRouter()

        async def cleared_handler() -> None:
            raise AssertionError("cleared, should not run")

        router.set_restart_handler(cleared_handler)
        # Now clear.
        router.set_restart_handler(None)

        result = await router.dispatch(_msg(body="/restart"), session)
        assert result == "handled"
        # Back to ack-only semantic.
        assert _send_calls(mcp) == []
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_set_restart_handler_rejects_non_callable(self) -> None:
        router = CommandRouter()
        with pytest.raises(TypeError, match="callable"):
            router.set_restart_handler("not callable")  # type: ignore[arg-type]


class TestDispatchPassthrough:
    async def test_passthrough_yields(self) -> None:
        session, mcp = _session()
        router = CommandRouter()
        router.passthrough("/explain")
        result = await router.dispatch(_msg(body="/explain me factorial"), session)
        assert result == "yield"
        assert _send_calls(mcp) == []  # SDK didn't reply
        assert _ack_calls(mcp) == []  # consumer must ack

    async def test_handler_wins_over_passthrough(self) -> None:
        # If both registered, user handler takes precedence.
        session, mcp = _session()
        router = CommandRouter()
        router.passthrough("/dual")

        @router.command("/dual")
        async def handle_dual(msg, hub, args):
            return "handled-not-yielded"

        result = await router.dispatch(_msg(body="/dual"), session)
        assert result == "handled"
        assert _send_calls(mcp) == [
            {"to": "@alice", "message": "handled-not-yielded"}
        ]


class TestDispatchUnknown:
    async def test_unknown_reject_default(self) -> None:
        session, mcp = _session()
        router = CommandRouter()  # unknown="reject" by default
        result = await router.dispatch(_msg(body="/foobar"), session)
        assert result == "handled"
        assert _send_calls(mcp) == [
            {"to": "@alice", "message": "command not found: /foobar"}
        ]
        assert _ack_calls(mcp) == [{"message_id": "m1"}]

    async def test_unknown_yield(self) -> None:
        session, mcp = _session()
        router = CommandRouter(unknown="yield")
        result = await router.dispatch(_msg(body="/foobar"), session)
        assert result == "yield"
        # No reply, no ack from the router
        assert _send_calls(mcp) == []
        assert _ack_calls(mcp) == []

    async def test_reject_format_customisable(self) -> None:
        session, mcp = _session()
        router = CommandRouter(reject_format="unknown command: {cmd}")
        await router.dispatch(_msg(body="/foobar"), session)
        assert _send_calls(mcp) == [
            {"to": "@alice", "message": "unknown command: /foobar"}
        ]


# ---------------------------------------------------------------------
# Integration with HubSession.inbox(commands=)
# ---------------------------------------------------------------------


class TestHubInboxCommandsKwarg:
    """``HubSession.inbox(commands=router)`` integration smoke tests."""

    async def test_inbox_with_router_intercepts_ping(self) -> None:
        # Startup drain returns one /ping. With ``commands=CommandRouter()``,
        # the router intercepts, replies, acks; iterator yields nothing.
        _recv_send, recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
        mcp = AsyncMock()

        unread_batches = [
            [
                {
                    "id": "m1",
                    "from": "@op",
                    "to": "@me",
                    "message": "/ping",
                    "timestamp": "2026-05-20T10:00:00Z",
                }
            ],
            [],  # subsequent drains empty
        ]

        async def call_tool(name, args):
            if name == "get_messages":
                return _text_result(json.dumps(unread_batches.pop(0)) if unread_batches else "[]")
            return _text_result("ok")

        mcp.call_tool = AsyncMock(side_effect=call_tool)
        mcp.subscribe_resource = AsyncMock()
        mcp.list_tools = AsyncMock(return_value=object())
        session = HubSession(session=mcp, config=_config(), notify_recv=recv)

        router = CommandRouter()
        received: list[str] = []

        async def consume():
            async with session.inbox(
                commands=router,
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                # Wait briefly to ensure startup drain completes, then
                # exit even if no messages — /ping should never yield.
                with anyio.move_on_after(0.2):
                    async for msg in messages:
                        received.append(msg.body)
                        break

        with anyio.fail_after(2.0):
            await consume()

        assert received == []  # /ping never yielded
        # /pong reply + ack issued by router
        sends = [c for c in mcp.call_tool.await_args_list if c.args[0] == "send_message"]
        assert any(c.args[1] == {"to": "@op", "message": "pong"} for c in sends)

    async def test_inbox_with_router_yields_natural_language(self) -> None:
        # Startup drain returns a non-command message; iterator yields it.
        _recv_send, recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
        mcp = AsyncMock()
        unread_batches = [
            [
                {
                    "id": "m1",
                    "from": "@alice",
                    "to": "@me",
                    "message": "hello there",
                    "timestamp": "2026-05-20T10:00:00Z",
                }
            ],
        ]

        async def call_tool(name, args):
            if name == "get_messages":
                return _text_result(json.dumps(unread_batches.pop(0)) if unread_batches else "[]")
            return _text_result("ok")

        mcp.call_tool = AsyncMock(side_effect=call_tool)
        mcp.subscribe_resource = AsyncMock()
        mcp.list_tools = AsyncMock(return_value=object())
        session = HubSession(session=mcp, config=_config(), notify_recv=recv)

        router = CommandRouter()
        received: list[str] = []

        async def consume():
            async with session.inbox(
                commands=router,
                poll_interval_s=3600.0,
                heartbeat_interval_s=3600.0,
            ) as messages:
                async for msg in messages:
                    received.append(msg.body)
                    break

        with anyio.fail_after(2.0):
            await consume()

        assert received == ["hello there"]
