"""Coverage for the M3 ``HubSession.one_shot()`` API.

``one_shot`` is the SDK's "open a fresh MCP session for one batch"
context manager — exactly the shape ``client-litellm`` and other
stateless workers want for transactional read-then-write pairs.

These tests don't reach the streamable-HTTP transport. They patch
:meth:`HubSession.open` so the test can assert *how* it's called (= the
fresh-session lifecycle) without needing a live agent-hub. The
:meth:`HubSession.open` integration itself is exercised by the M0/M1
smoke tests and by every real bridge that consumes the SDK.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp import types

from agent_hub_sdk import AgentHub, ConfigurationError, HubSession
from agent_hub_sdk.config import Config


def _config(*, user: str = "me") -> Config:
    return Config(
        user=user,
        display_name=None,
        mode="stateless",
        tenant=None,
        url="https://hub.example/mcp",
        pat="ghp_xxx",
    )


def _session(config: Config | None = None) -> HubSession:
    """Build a HubSession with a mocked MCP session + closed push queue.

    M5 (issue #27): ``AgentHub.connect()`` now auto-registers, which
    means any test that goes through ``connect`` (`test_nested_inside_connect`
    in particular) routes a ``call_tool("register", ...)`` through this
    mock. Without a stubbed ``call_tool``, the bare ``AsyncMock`` returns
    a ``MagicMock`` whose ``.isError`` is truthy → ``raise_for_tool_error``
    raises ``RuntimeError: register failed: (no detail)``.

    Stub ``call_tool`` with a success-shaped ``CallToolResult`` so
    auto-register passes. Existing ``TestOneShotLifecycle`` tests
    assert lifecycle (open/yield/close) rather than the call shape, so
    this stub does not affect them.
    """
    _send, recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
    mock_mcp = AsyncMock()
    mock_mcp.call_tool = AsyncMock(
        return_value=types.CallToolResult(
            content=[types.TextContent(type="text", text="registered")],
            isError=False,
        )
    )
    return HubSession(
        session=mock_mcp,
        config=config or _config(),
        notify_recv=recv,
    )


class TestOneShotLifecycle:
    """``one_shot`` opens, yields, and closes a fresh session."""

    async def test_yields_fresh_hub_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outer = _session()
        config_seen: list[Config] = []
        opened: list[HubSession] = []

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            config_seen.append(config)
            fresh = _session(config)
            opened.append(fresh)
            yield fresh

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with outer.one_shot() as inner:
            # ``inner`` is a HubSession (and a *different* instance from outer).
            assert isinstance(inner, HubSession)
            assert inner is not outer
            assert inner is opened[0]

        # ``open`` was called exactly once with the outer's config.
        assert config_seen == [outer._config]

    async def test_inner_uses_outer_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stateless caller's config carries through. The fresh session
        # must see the same URL, PAT, tenant, etc. — otherwise it's
        # opening against a different hub.
        cfg = Config(
            user="translator",
            display_name="JP/EN translator",
            mode="stateless",
            tenant="kaz",
            url="https://hub.example/mcp",
            pat="ghp_xyz",
        )
        outer = _session(cfg)
        seen: list[Config] = []

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            seen.append(config)
            yield _session(config)

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with outer.one_shot():
            pass

        assert seen == [cfg]
        assert seen[0].user == "translator"
        assert seen[0].tenant == "kaz"
        assert seen[0].mode == "stateless"

    async def test_cleanup_runs_on_normal_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outer = _session()
        events: list[str] = []

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            events.append("enter")
            try:
                yield _session(config)
            finally:
                events.append("exit")

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with outer.one_shot():
            events.append("body")

        assert events == ["enter", "body", "exit"]

    async def test_cleanup_runs_on_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outer = _session()
        events: list[str] = []

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            events.append("enter")
            try:
                yield _session(config)
            finally:
                events.append("exit")

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        with pytest.raises(RuntimeError, match="boom"):
            async with outer.one_shot():
                events.append("body")
                raise RuntimeError("boom")

        # Cleanup must run even when the body raises.
        assert events == ["enter", "body", "exit"]


class TestOneShotIndependence:
    """The inner ``HubSession`` is genuinely separate from the outer."""

    async def test_inner_session_object_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outer = _session()

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            yield _session(config)

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with outer.one_shot() as inner:
            assert inner._session is not outer._session

    async def test_inner_status_starts_at_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Outer sets a non-default status; the inner one_shot session
        # should start fresh at "idle" (= /status carry-over is NOT a
        # feature, per the one_shot docstring).
        outer = _session()
        outer.set_status("busy")

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            yield _session(config)

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with outer.one_shot() as inner:
            assert inner._status == "idle"

        # Outer's status is unaffected by the inner block.
        assert outer._status == "busy"

    async def test_inner_push_queue_is_separate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An item placed on the outer's push queue is NOT visible to
        # the inner session (= they share no notification state).
        outer = _session()
        send, _recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
        # Replace outer's recv to control what's queued (we'll prove the
        # inner doesn't see it).
        outer._notify_recv = _recv  # type: ignore[assignment]
        send.send_nowait("inbox://@outer-only")

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            yield _session(config)

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with outer.one_shot() as inner:
            # Inner's recv stream is its own; nothing on it yet.
            # Drain attempt should not see the outer-only URI.
            with anyio.move_on_after(0.05):
                async for uri in inner.inbox_pushes():
                    raise AssertionError(
                        f"inner session should not see outer's push {uri!r}"
                    )


class TestOneShotNested:
    """``one_shot`` is usable inside ``AgentHub.connect``'s lifetime."""

    async def test_nested_inside_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity-check that the typical stateless pattern compiles and
        # runs: ``AgentHub.connect`` (= long-lived) wrapping
        # ``hub.one_shot`` (= per-batch). Patches ``HubSession.open`` so
        # the test doesn't hit a real agent-hub.
        opens: list[Config] = []

        @asynccontextmanager
        async def fake_open(cls, config: Config):
            opens.append(config)
            yield _session(config)

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        async with AgentHub.connect(
            user="translator",
            mode="stateless",
            url="https://hub.example/mcp",
            pat="ghp_xxx",
        ) as outer_hub:
            async with outer_hub.one_shot() as inner_hub:
                assert inner_hub is not outer_hub

        # Two ``open`` calls: one for AgentHub.connect, one for the
        # nested one_shot.
        assert len(opens) == 2
        assert opens[0] == opens[1]  # same Config passed both times


class TestOneShotConfigPassthrough:
    """``one_shot`` rejects misconfiguration the same way as the outer hub."""

    async def test_no_config_no_problem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity: a properly configured outer hub yields a one_shot
        # that doesn't surprise the consumer with a ConfigurationError.
        @asynccontextmanager
        async def fake_open(cls, config: Config):
            yield _session(config)

        monkeypatch.setattr(HubSession, "open", classmethod(fake_open))

        outer = _session()
        async with outer.one_shot() as inner:
            assert inner._config.url == outer._config.url

    async def test_agent_hub_connect_still_fail_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # one_shot is built on top of HubSession.open which expects a
        # valid Config. ConfigurationError continues to fail-fast at
        # AgentHub.connect — one_shot doesn't soften that contract.
        monkeypatch.delenv("AGENT_HUB_URL", raising=False)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        with pytest.raises(ConfigurationError):
            async with AgentHub.connect(user="me", url=None, pat=None):
                pass  # pragma: no cover — should never reach
