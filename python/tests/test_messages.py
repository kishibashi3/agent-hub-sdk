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
