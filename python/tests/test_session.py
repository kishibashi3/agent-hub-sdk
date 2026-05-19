"""``HubSession`` behaviour with a mocked MCP ``ClientSession``.

These tests don't reach the streamable-HTTP transport; they construct
``HubSession`` directly with a stub session and a closed push queue. The
``HubSession.open`` async context manager (which does talk to MCP) is
out of scope for unit tests — it's exercised by the bridge-slack dogfood
in a later milestone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest
from mcp import types

from agent_hub_sdk import (
    AgentHub,
    ConfigurationError,
    HubTransientError,
    PeerNotFoundError,
)
from agent_hub_sdk.config import Config
from agent_hub_sdk.session import HubSession


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


@pytest.fixture
def config() -> Config:
    return Config(
        user="me",
        display_name="Tester",
        mode="stateful",
        tenant=None,
        url="https://hub.example/mcp",
        pat="ghp_xxx",
    )


@pytest.fixture
def session(config: Config) -> HubSession:
    """A HubSession with a mocked MCP session and a closed push queue."""
    mock_mcp = AsyncMock()
    _, recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
    return HubSession(session=mock_mcp, config=config, notify_recv=recv)


class TestRegister:
    async def test_register_calls_tool_with_mode(
        self, session: HubSession
    ) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("registered: @me")
        )
        text = await session.register()
        assert text == "registered: @me"
        session._session.call_tool.assert_awaited_once_with(
            "register",
            {"name": "me", "display_name": "Tester", "mode": "stateful"},
        )

    async def test_register_propagates_unknown_isError(
        self, session: HubSession
    ) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("permission denied", is_error=True)
        )
        with pytest.raises(RuntimeError, match="register"):
            await session.register()


class TestSend:
    async def test_send_success(self, session: HubSession) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("delivered")
        )
        text = await session.send("@peer", "hello")
        assert text == "delivered"
        session._session.call_tool.assert_awaited_once_with(
            "send_message", {"to": "@peer", "message": "hello"}
        )

    async def test_send_peer_not_found(self, session: HubSession) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result(
                "宛先 @gemma は存在しません", is_error=True
            )
        )
        with pytest.raises(PeerNotFoundError) as exc_info:
            await session.send("@gemma", "hi")
        assert exc_info.value.peer == "@gemma"

    async def test_send_transient(self, session: HubSession) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("503 service unavailable", is_error=True)
        )
        with pytest.raises(HubTransientError):
            await session.send("@peer", "hi")

    async def test_send_transport_failure_becomes_transient(
        self, session: HubSession
    ) -> None:
        session._session.call_tool = AsyncMock(
            side_effect=ConnectionError("connection refused")
        )
        with pytest.raises(HubTransientError, match="transport failure"):
            await session.send("@peer", "hi")


class TestSendWithRetry:
    async def test_succeeds_on_first_try(self, session: HubSession) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("delivered")
        )
        text = await session.send_with_retry("@peer", "hi")
        assert text == "delivered"
        assert session._session.call_tool.await_count == 1

    async def test_retries_transient_then_succeeds(
        self, session: HubSession
    ) -> None:
        # First two attempts: transient; third: success.
        results = [
            _text_result("503", is_error=True),
            _text_result("timed out", is_error=True),
            _text_result("delivered"),
        ]
        session._session.call_tool = AsyncMock(side_effect=results)
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        text = await session.send_with_retry(
            "@peer", "hi", base_delay_s=1.0, sleep_fn=fake_sleep
        )
        assert text == "delivered"
        # Two sleeps between three attempts, exponential.
        assert slept == [1.0, 2.0]

    async def test_gives_up_after_max_attempts(
        self, session: HubSession
    ) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("503", is_error=True)
        )

        async def fake_sleep(delay: float) -> None:
            pass

        with pytest.raises(HubTransientError):
            await session.send_with_retry(
                "@peer", "hi", max_attempts=3, sleep_fn=fake_sleep
            )
        assert session._session.call_tool.await_count == 3

    async def test_peer_not_found_skips_retry(
        self, session: HubSession
    ) -> None:
        # Peer-not-found is non-retriable — first attempt should raise
        # without sleeping or calling again.
        session._session.call_tool = AsyncMock(
            return_value=_text_result("not registered", is_error=True)
        )
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        with pytest.raises(PeerNotFoundError):
            await session.send_with_retry(
                "@peer", "hi", sleep_fn=fake_sleep
            )
        assert session._session.call_tool.await_count == 1
        assert slept == []


class TestInboxOperations:
    async def test_get_unread_parses_payload(
        self, session: HubSession
    ) -> None:
        payload = (
            '[{"id":"m1","from":"@a","to":"@me",'
            '"message":"hi","timestamp":"2026-05-19T10:00:00Z"}]'
        )
        session._session.call_tool = AsyncMock(
            return_value=_text_result(payload)
        )
        msgs = await session.get_unread()
        assert len(msgs) == 1
        assert msgs[0].id == "m1"
        assert msgs[0].sender == "@a"
        assert msgs[0].body == "hi"

    async def test_ack_calls_mark_as_read(self, session: HubSession) -> None:
        session._session.call_tool = AsyncMock(
            return_value=_text_result("")
        )
        await session.ack("m1")
        session._session.call_tool.assert_awaited_once_with(
            "mark_as_read", {"message_id": "m1"}
        )

    async def test_subscribe_inbox_uses_self_uri(
        self, session: HubSession
    ) -> None:
        session._session.subscribe_resource = AsyncMock()
        await session.subscribe_inbox()
        # ``AnyUrl(f"inbox://@{user}")`` parses ``@`` as a (missing)
        # userinfo separator and round-trips as ``inbox://<user>``. The
        # agent-hub server has historically accepted both forms (the
        # original bridge-slack / bridge-claude HubClient uses this exact
        # idiom), so we preserve the existing wire behaviour rather than
        # try to round-trip the literal ``@``.
        call = session._session.subscribe_resource.await_args
        assert call is not None
        (uri_arg,) = call.args
        assert str(uri_arg) == "inbox://me"

    async def test_get_participants_filters_teams(
        self, session: HubSession
    ) -> None:
        payload = (
            '[{"type":"person","name":"@a","display_name":"A",'
            '"mode":"stateful","is_online":true},'
            '{"type":"team","name":"@discussion","members":["@a"]}]'
        )
        session._session.call_tool = AsyncMock(
            return_value=_text_result(payload)
        )
        participants = await session.get_participants()
        assert len(participants) == 1
        assert participants[0].name == "@a"

    async def test_heartbeat_calls_list_tools(
        self, session: HubSession
    ) -> None:
        session._session.list_tools = AsyncMock(return_value=object())
        await session.heartbeat()
        session._session.list_tools.assert_awaited_once_with()


class TestAgentHubConnect:
    async def test_connect_fails_fast_on_missing_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No env, no kwargs → ConfigurationError before any network I/O.
        # Clear both env vars in case the local dev env has them set.
        monkeypatch.delenv("AGENT_HUB_URL", raising=False)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        with pytest.raises(ConfigurationError):
            async with AgentHub.connect(user="me", url=None, pat=None):
                pass  # pragma: no cover — should never reach
