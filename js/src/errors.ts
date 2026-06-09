// Error taxonomy mirroring the Python ``agent_hub_sdk/errors.py``. Three
// concrete error classes plus a classifier that buckets raw agent-hub
// server error strings into one of three kinds. EN + JA patterns are
// preserved verbatim from the Python side — agent-hub server today
// returns Japanese error text in production (e.g. ``宛先 @list は存在しません``).

/**
 * Required SDK configuration is missing.
 *
 * Raised when both the environment variable and the ``AgentHub.connect``
 * keyword argument for a required field (``url`` or ``pat``) are absent.
 *
 * **Redline**: the SDK does not fall back to ``localhost:3000`` or any
 * other implicit default URL. Silently pointing at the wrong hub is
 * worse than refusing to start.
 */
export class ConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigurationError";
    // Maintain proper prototype chain for ``instanceof`` checks across
    // transpilation targets.
    Object.setPrototypeOf(this, ConfigurationError.prototype);
  }
}

/**
 * ``send`` target participant is not registered on the hub or is offline.
 *
 * Retry is meaningless. Surfacing this as a distinct class lets a
 * bridge (e.g. VS Code) react with a user-visible warning instead of
 * a generic transient error.
 */
export class ParticipantNotFoundError extends Error {
  readonly peer: string;
  readonly detail: string;

  constructor(peer: string, detail: string) {
    super(`peer ${peer} not found on agent-hub: ${detail}`);
    this.name = "ParticipantNotFoundError";
    this.peer = peer;
    this.detail = detail;
    Object.setPrototypeOf(this, ParticipantNotFoundError.prototype);
  }
}

/**
 * The hub is temporarily unavailable (5xx / network / timeout).
 *
 * Retry-with-backoff is appropriate. ``sendWithRetry`` will do this
 * automatically; if the retry budget is exhausted it re-throws so the
 * caller can surface the failure.
 */
export class HubTransientError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "HubTransientError";
    Object.setPrototypeOf(this, HubTransientError.prototype);
  }
}

/**
 * Classification of an agent-hub error string. Mirrors Python's
 * ``HubErrorKind`` literal type.
 */
export type HubErrorKind = "participant_not_found" | "transient" | "unknown";

// Patterns for "peer not found" / "peer offline" agent-hub error
// strings. Includes both English and Japanese because the production
// server returns Japanese strings today. Matching is case-insensitive
// substring (Unicode substring works without ``toLowerCase()`` for the
// Japanese half).
const PEER_NOT_FOUND_PATTERNS = [
  // English
  "not found",
  "not online",
  "no such peer",
  "unknown peer",
  "does not exist",
  "not registered",
  "offline",
  // Japanese (observed in production)
  "存在しません",
  "見つかりません",
  "登録されていません",
  "オフライン",
] as const;

// Patterns for transient (= retry-worthy) agent-hub error strings.
const TRANSIENT_PATTERNS = [
  // English
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
  // Japanese
  "タイムアウト",
  "一時的",
  "応答していません",
  "再試行",
] as const;

/**
 * Bucket a raw agent-hub error string into one of three kinds.
 *
 * Returns ``"unknown"`` when no pattern matches — better to surface an
 * unhandled error than to misclassify and silently retry forever.
 *
 * @param errorText the raw error text from an ``isError=true`` tool
 *   result, or ``null`` / ``undefined`` if no body was present.
 */
export function classifyHubError(
  errorText: string | null | undefined,
): HubErrorKind {
  if (!errorText) {
    return "unknown";
  }
  const textLower = errorText.toLowerCase();
  for (const pattern of PEER_NOT_FOUND_PATTERNS) {
    if (textLower.includes(pattern)) {
      return "participant_not_found";
    }
  }
  for (const pattern of TRANSIENT_PATTERNS) {
    if (textLower.includes(pattern)) {
      return "transient";
    }
  }
  return "unknown";
}
