"""Parser coverage for ``get_messages`` and ``get_participants`` JSON shapes."""

from __future__ import annotations

import json

import pytest

from agent_hub_sdk import IncomingMessage, Participant
from agent_hub_sdk.messages import parse_messages, parse_participants


class TestParseMessages:
    def test_parses_well_formed_batch(self) -> None:
        payload = json.dumps(
            [
                {
                    "id": "msg-1",
                    "from": "@alice",
                    "to": "@me",
                    "message": "hello",
                    "timestamp": "2026-05-19T10:00:00Z",
                },
                {
                    "id": "msg-2",
                    "from": "@bob",
                    "to": "@me",
                    "message": "second",
                    "timestamp": "2026-05-19T10:00:01Z",
                },
            ]
        )
        result = parse_messages(payload)
        assert len(result) == 2
        assert result[0] == IncomingMessage(
            id="msg-1",
            sender="@alice",
            to="@me",
            body="hello",
            timestamp="2026-05-19T10:00:00Z",
        )

    def test_parses_caused_by_when_present(self) -> None:
        """v10: caused_by フィールドが存在する場合は IncomingMessage に含まれる (issue #37)."""
        payload = json.dumps(
            [
                {
                    "id": "reply-id",
                    "from": "@alice",
                    "to": "@me",
                    "message": "got it",
                    "timestamp": "2026-05-30T00:00:01Z",
                    "caused_by": "root-id",
                },
            ]
        )
        result = parse_messages(payload)
        assert len(result) == 1
        assert result[0].caused_by == "root-id"

    def test_caused_by_defaults_to_none_when_absent(self) -> None:
        """caused_by フィールドがない (pre-v10 / root メッセージ) は None (issue #37)."""
        payload = json.dumps(
            [
                {
                    "id": "msg-1",
                    "from": "@alice",
                    "to": "@me",
                    "message": "hello",
                    "timestamp": "2026-05-30T00:00:00Z",
                },
            ]
        )
        result = parse_messages(payload)
        assert result[0].caused_by is None

    def test_caused_by_non_string_treated_as_none(self) -> None:
        """caused_by が文字列でない場合 (null / 数値 / bool) は None に正規化する (issue #37)."""
        for bad_value in [None, 42, True, [], {}]:
            payload = json.dumps(
                [
                    {
                        "id": "msg-1",
                        "from": "@alice",
                        "to": "@me",
                        "message": "x",
                        "timestamp": "2026-05-30T00:00:00Z",
                        "caused_by": bad_value,
                    },
                ]
            )
            result = parse_messages(payload)
            assert result[0].caused_by is None, f"caused_by={bad_value!r} should map to None"

    def test_skips_malformed_entries(self) -> None:
        # One good, one with missing key, one with non-dict shape.
        payload = json.dumps(
            [
                {
                    "id": "ok",
                    "from": "@a",
                    "to": "@me",
                    "message": "yes",
                    "timestamp": "2026-05-19T10:00:00Z",
                },
                {"id": "bad", "from": "@a"},  # missing keys
                "not a dict",
            ]
        )
        result = parse_messages(payload)
        assert len(result) == 1
        assert result[0].id == "ok"

    def test_top_level_non_list_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Unexpected"):
            parse_messages(json.dumps({"id": "msg-1"}))


class TestParseParticipants:
    def test_filters_to_persons(self) -> None:
        payload = json.dumps(
            [
                {
                    "type": "person",
                    "name": "@alice",
                    "display_name": "Alice the planner",
                    "mode": "stateful",
                    "is_online": True,
                },
                {
                    "type": "team",
                    "name": "@discussion",
                    "members": ["@alice"],
                },
                {
                    "type": "person",
                    "name": "@bob",
                    "display_name": None,
                    "mode": None,
                    "is_online": False,
                },
            ]
        )
        result = parse_participants(payload)
        assert len(result) == 2
        assert result[0] == Participant(
            name="@alice",
            display_name="Alice the planner",
            mode="stateful",
            is_online=True,
        )
        assert result[1] == Participant(
            name="@bob",
            display_name=None,
            mode=None,
            is_online=False,
        )

    def test_handles_partial_drift(self) -> None:
        # Missing name → skip; non-string display_name → None; missing
        # is_online → False. Server schema drift shouldn't crash the list.
        payload = json.dumps(
            [
                {"type": "person", "name": "@a"},  # minimal but valid
                {"type": "person"},  # missing name → skip
                {"type": "person", "name": ""},  # empty name → skip
                {"type": "person", "name": "@b", "display_name": 42},  # bad type
                "stray scalar",  # not a dict → skip
            ]
        )
        result = parse_participants(payload)
        assert [p.name for p in result] == ["@a", "@b"]
        assert result[0].is_online is False  # default when missing
        assert result[1].display_name is None  # non-string ignored

    def test_top_level_non_list_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Unexpected"):
            parse_participants(json.dumps({"name": "@a"}))
