"""Coverage for the error taxonomy and classifier."""

from __future__ import annotations

import pytest

from agent_hub_sdk import (
    ConfigurationError,
    HubTransientError,
    PeerNotFoundError,
    classify_hub_error,
)


class TestClassifyHubError:
    """``classify_hub_error`` recognises both English and Japanese phrases."""

    @pytest.mark.parametrize(
        "text",
        [
            "peer @gemma not found",
            "no such peer",
            "@bot is offline",
            "Not Registered",  # case-insensitive
            "宛先 @list は存在しません",  # observed in production
            "peer が見つかりません",
            "@gemma は登録されていません",
            "オフライン状態です",
        ],
    )
    def test_peer_not_found(self, text: str) -> None:
        assert classify_hub_error(text) == "peer_not_found"

    @pytest.mark.parametrize(
        "text",
        [
            "HTTP 502 Bad Gateway",
            "503 service unavailable",
            "504 Gateway Timeout",
            "request timed out",
            "connection refused",
            "network unreachable",
            "please try again later",
            "agent-hub が応答していません",
            "タイムアウトしました",
            "一時的なエラー",
            "しばらくしてから再試行してください",
        ],
    )
    def test_transient(self, text: str) -> None:
        assert classify_hub_error(text) == "transient"

    @pytest.mark.parametrize(
        "text",
        [
            "schema validation failed",
            "permission denied",
            "internal logic error: foo > bar",
            "",
            None,
        ],
    )
    def test_unknown_or_empty(self, text: str | None) -> None:
        assert classify_hub_error(text) == "unknown"


class TestErrorClasses:
    """The custom error classes carry useful structured detail."""

    def test_peer_not_found_carries_peer_and_detail(self) -> None:
        exc = PeerNotFoundError("@gemma", "offline")
        assert exc.peer == "@gemma"
        assert exc.detail == "offline"
        assert "@gemma" in str(exc)
        assert "offline" in str(exc)

    def test_hub_transient_is_runtime_error(self) -> None:
        # Subclass of RuntimeError so legacy `except RuntimeError:` still
        # catches it. Important for migration from bridge-slack/hub.py.
        assert issubclass(HubTransientError, RuntimeError)

    def test_configuration_error_is_runtime_error(self) -> None:
        # Same reasoning — bridge-slack today raises RuntimeError on
        # missing env. The new class is more specific but stays catchable
        # by existing handlers.
        assert issubclass(ConfigurationError, RuntimeError)
