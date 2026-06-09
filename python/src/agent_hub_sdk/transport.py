"""Low-level MCP tool-call helpers shared by the session layer.

These functions extract content from ``CallToolResult`` records and route
``isError=True`` responses through :func:`classify_hub_error` so callers
deal in :class:`ParticipantNotFoundError` / :class:`HubTransientError` rather than
inspecting tool-result blobs by hand.

Tool transport itself (MCP ``ClientSession`` over streamable HTTP) lives in
:mod:`agent_hub_sdk.session`; this module is a pure helper layer that takes
already-issued tool results and gives back typed Python values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_hub_sdk.errors import (
    HubTransientError,
    ParticipantNotFoundError,
    classify_hub_error,
)

if TYPE_CHECKING:
    from mcp import types

__all__ = [
    "extract_text",
    "raise_for_send_error",
    "raise_for_tool_error",
]


def extract_text(content: list[types.ContentBlock] | None) -> str:
    """Concatenate the text content of an MCP tool result.

    Non-text blocks (images, resources) are silently ignored; the agent-hub
    tools we use only return text today, so this is a deliberate shortcut.
    """
    if not content:
        return ""
    from mcp import types  # local import — avoid a hard MCP import at module load

    parts: list[str] = []
    for block in content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
    return "\n".join(parts)


def raise_for_tool_error(
    result: types.CallToolResult, *, op: str
) -> str:
    """Raise the right error class for an MCP ``CallToolResult``.

    On success returns the joined text content. On ``isError=True`` the
    error text is run through :func:`classify_hub_error`; transient errors
    become :class:`HubTransientError`, anything else becomes a generic
    :class:`RuntimeError` (the caller decides whether to special-case it).

    Peer-not-found is **not** raised here — only ``send`` cares about that
    distinction. Use :func:`raise_for_send_error` from the send path.

    :param op: human-readable operation name for the error message.
    """
    text = extract_text(result.content)
    if not result.isError:
        return text
    kind = classify_hub_error(text)
    if kind == "transient":
        raise HubTransientError(f"{op} transient: {text or '(no detail)'}")
    raise RuntimeError(f"{op} failed: {text or '(no detail)'}")


def raise_for_send_error(
    result: types.CallToolResult, *, to: str
) -> str:
    """``send``-specific variant that also surfaces peer-not-found.

    Same shape as :func:`raise_for_tool_error` but classifies ``isError``
    payloads into three buckets:

    - participant_not_found → :class:`ParticipantNotFoundError`
    - transient → :class:`HubTransientError`
    - anything else → :class:`RuntimeError`

    :param to: the ``send`` recipient handle, used to populate
        ``ParticipantNotFoundError.peer``.
    """
    text = extract_text(result.content)
    if not result.isError:
        return text
    kind = classify_hub_error(text)
    if kind == "participant_not_found":
        raise ParticipantNotFoundError(peer=to, detail=text or "(no detail)")
    if kind == "transient":
        raise HubTransientError(f"send to {to} transient: {text or '(no detail)'}")
    raise RuntimeError(f"send to {to} failed: {text or '(no detail)'}")
