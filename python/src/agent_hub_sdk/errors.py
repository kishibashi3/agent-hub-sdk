"""Error taxonomy for agent-hub-sdk.

Three custom errors plus a classifier:

- ``ConfigurationError`` — required config (``url`` / ``pat``) missing from both
  env and caller args. **Fail-fast**: no ``localhost:3000`` fallback, no
  implicit default URL. (See ``docs/design.md`` §4 — this is redline #1.)
- ``ParticipantNotFoundError`` — the ``send`` target is not registered on the hub or
  is offline. No retry is meaningful.
- ``HubTransientError`` — server 5xx / network / timeout. Retry-with-backoff
  is appropriate. ``send_with_retry`` does this for you; if the retry budget
  is exhausted, it re-raises so the caller can surface the failure (e.g. a
  Slack bridge posts a "hub temporarily unavailable" notice).

Plus ``classify_hub_error(text)`` which buckets a raw agent-hub error message
into one of those kinds. The patterns include both English and Japanese
because the production agent-hub server today returns Japanese error strings
(e.g. ``宛先 @list は存在しません``); see ``bridge-slack`` routing.py for the
historical evolution of this list.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "ConfigurationError",
    "HubErrorKind",
    "HubTransientError",
    "ParticipantNotFoundError",
    "classify_hub_error",
]


class ConfigurationError(RuntimeError):
    """Required SDK configuration is missing.

    Raised when both the environment variable and the ``AgentHub.connect``
    keyword argument for a required field (``url`` or ``pat``) are absent.

    **Redline**: the SDK does not fall back to ``localhost:3000`` or any other
    implicit default URL. Silently pointing at the wrong hub is worse than
    refusing to start; the consumer (CLI, bridge, plugin) is expected to
    surface a clear "set ``AGENT_HUB_URL`` or pass ``url=``" message.
    """


class ParticipantNotFoundError(RuntimeError):
    """``send`` target participant is not registered on the hub or is offline.

    Retry is meaningless — the participant is not going to materialise from a
    retry loop. Surfacing this as a distinct class lets a bridge (e.g. Slack)
    react with a Slack-visible warning like "``@gemma`` is not registered"
    instead of a generic transient error.
    """

    def __init__(self, peer: str, detail: str) -> None:
        self.peer = peer
        self.detail = detail
        super().__init__(f"peer {peer} not found on agent-hub: {detail}")


class HubTransientError(RuntimeError):
    """The hub is temporarily unavailable (5xx / network / timeout).

    Retry-with-backoff is appropriate. ``send_with_retry`` will do this
    automatically; if the retry budget is exhausted it re-raises so the
    caller can surface the failure (e.g. a Slack bridge posts a
    "hub temporarily unavailable" thread notice).
    """


HubErrorKind = Literal["participant_not_found", "transient", "unknown"]

# Patterns for "peer not found" / "peer offline" agent-hub error strings.
# Includes both English and Japanese because the production server returns
# Japanese strings today; matching is case-insensitive substring (Unicode
# substring works without ``lower()`` for the Japanese half).
_PEER_NOT_FOUND_PATTERNS = (
    # English
    "not found",
    "not online",
    "no such peer",
    "unknown peer",
    "does not exist",
    "not registered",
    "offline",
    # Japanese (observed in production)
    "存在しません",
    "見つかりません",
    "登録されていません",
    "オフライン",
)

# Patterns for transient (= retry-worthy) agent-hub error strings.
_TRANSIENT_PATTERNS = (
    # English
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporarily",
    "try again",
    "service unavailable",
    "bad gateway",
    # Japanese
    "タイムアウト",
    "一時的",
    "応答していません",
    "再試行",
)


def classify_hub_error(error_text: str | None) -> HubErrorKind:
    """Bucket a raw agent-hub error string into one of three kinds.

    The agent-hub server's error wording is not strictly specified, so the
    classifier uses substring matching against curated patterns. When in
    doubt it returns ``"unknown"`` — better to surface an unhandled error
    than to misclassify and silently retry forever.

    :param error_text: the raw error text from an ``isError=True`` tool
        result, or ``None`` if no body was present.
    :returns: one of ``"participant_not_found"``, ``"transient"``, ``"unknown"``.
    """
    if not error_text:
        return "unknown"
    text_lower = error_text.lower()
    if any(p in text_lower for p in _PEER_NOT_FOUND_PATTERNS):
        return "participant_not_found"
    if any(p in text_lower for p in _TRANSIENT_PATTERNS):
        return "transient"
    return "unknown"
