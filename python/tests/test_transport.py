"""Coverage for the tool-result classification helpers in ``transport``."""

from __future__ import annotations

import pytest
from mcp import types

from agent_hub_sdk import HubTransientError, ParticipantNotFoundError
from agent_hub_sdk.transport import (
    extract_text,
    raise_for_send_error,
    raise_for_tool_error,
)


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    """Build a minimal ``CallToolResult`` with a single TextContent block."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


class TestExtractText:
    def test_joins_text_blocks(self) -> None:
        result = types.CallToolResult(
            content=[
                types.TextContent(type="text", text="hello"),
                types.TextContent(type="text", text="world"),
            ],
            isError=False,
        )
        assert extract_text(result.content) == "hello\nworld"

    def test_handles_empty_content(self) -> None:
        assert extract_text(None) == ""
        assert extract_text([]) == ""


class TestRaiseForToolError:
    def test_success_returns_text(self) -> None:
        result = _text_result("ok body")
        assert raise_for_tool_error(result, op="register") == "ok body"

    def test_transient_isError_raises_transient(self) -> None:
        result = _text_result("503 service unavailable", is_error=True)
        with pytest.raises(HubTransientError, match="register"):
            raise_for_tool_error(result, op="register")

    def test_unknown_isError_raises_runtime_error(self) -> None:
        # Non-transient, non-peer error → bare RuntimeError so caller has
        # to think about whether to special-case it.
        result = _text_result("schema mismatch foo", is_error=True)
        with pytest.raises(RuntimeError) as exc_info:
            raise_for_tool_error(result, op="register")
        # And specifically not one of the typed classes.
        assert not isinstance(exc_info.value, HubTransientError)


class TestRaiseForSendError:
    def test_success_returns_text(self) -> None:
        result = _text_result("delivered")
        assert raise_for_send_error(result, to="@peer") == "delivered"

    def test_peer_not_found_raises_peer_class(self) -> None:
        result = _text_result("宛先 @gemma は存在しません", is_error=True)
        with pytest.raises(ParticipantNotFoundError) as exc_info:
            raise_for_send_error(result, to="@gemma")
        assert exc_info.value.peer == "@gemma"
        assert "存在しません" in exc_info.value.detail

    def test_transient_raises_transient_class(self) -> None:
        result = _text_result("agent-hub が応答していません", is_error=True)
        with pytest.raises(HubTransientError, match="@gemma"):
            raise_for_send_error(result, to="@gemma")

    def test_unknown_raises_runtime_error(self) -> None:
        result = _text_result("policy violation: blocked", is_error=True)
        with pytest.raises(RuntimeError) as exc_info:
            raise_for_send_error(result, to="@gemma")
        assert not isinstance(exc_info.value, (ParticipantNotFoundError, HubTransientError))
