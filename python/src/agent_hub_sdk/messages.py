"""Dataclasses for agent-hub messages and participants, plus their JSON parsers.

The wire format mirrors the agent-hub MCP tools:

- ``get_messages`` returns a JSON array; each entry has the fields ``id``,
  ``from``, ``to``, ``message``, ``timestamp``. We rename ``from`` → ``sender``
  and ``message`` → ``body`` so consumers don't have to dodge Python's
  reserved word.
- ``get_participants`` returns a mixed JSON array of ``type == "person"`` and
  ``type == "team"``. The SDK exposes only the ``person`` entries; team
  surface is deferred to a later milestone.

Parsers are deliberately lenient: schema drift on the server side (extra
fields, occasional ``None`` where a string was expected) is logged and
ignored rather than raised, so a single malformed participant doesn't kill
the whole ``list`` operation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "IncomingMessage",
    "Participant",
    "parse_messages",
    "parse_participants",
]


@dataclass(frozen=True)
class IncomingMessage:
    """One unread message fetched via ``get_messages``.

    :param id: server-assigned UUID; pass back to ``ack`` to mark as read.
    :param sender: the ``@handle`` that sent the message.
    :param to: the recipient (``@self`` for DMs, ``@team`` for team
        broadcasts). Useful when the consumer is multiplexing roles.
    :param body: the human-readable message body.
    :param timestamp: ISO-8601 UTC timestamp string from the server.
    """

    id: str
    sender: str
    to: str
    body: str
    timestamp: str


@dataclass(frozen=True)
class Participant:
    """One ``person``-type participant in ``get_participants``.

    :param name: the participant's ``@handle``.
    :param display_name: optional role descriptor. ``None`` if the
        participant registered without one.
    :param mode: worker mode the participant declared at register-time
        (``stateful`` / ``stateless`` / ``global``). ``None`` if absent.
    :param is_online: whether the participant currently has an inbox
        subscription open. Useful for filtering ``list`` UIs to "who's
        actually reachable right now".
    """

    name: str
    display_name: str | None
    mode: str | None
    is_online: bool


def parse_messages(text: str) -> list[IncomingMessage]:
    """Parse the JSON body of ``get_messages`` into typed records.

    :param text: the JSON text from the tool result.
    :raises RuntimeError: if the top-level shape is not a JSON list.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected get_messages response: {data!r}")
    result: list[IncomingMessage] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        # Required keys; if any are missing or non-string, skip the entry
        # rather than crash the whole batch (= defensive against schema drift).
        try:
            result.append(
                IncomingMessage(
                    id=row["id"],
                    sender=row["from"],
                    to=row["to"],
                    body=row["message"],
                    timestamp=row["timestamp"],
                )
            )
        except (KeyError, TypeError):
            continue
    return result


def parse_participants(text: str) -> list[Participant]:
    """Parse the JSON body of ``get_participants``, keeping only persons.

    Team entries (``type == "team"``) are filtered out; team surface is
    deferred to a later milestone. Malformed entries are silently dropped
    so one bad row doesn't kill the whole list.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected get_participants response: {data!r}")
    result: list[Participant] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "person":
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        display_raw = row.get("display_name")
        mode_raw = row.get("mode")
        result.append(
            Participant(
                name=name,
                display_name=display_raw if isinstance(display_raw, str) else None,
                mode=mode_raw if isinstance(mode_raw, str) else None,
                is_online=bool(row.get("is_online", False)),
            )
        )
    return result
