// Dataclass-equivalents (= TypeScript interfaces) for agent-hub messages
// and participants, plus the JSON parsers that produce them. Mirrors
// Python's ``agent_hub_sdk/messages.py`` 1:1.
//
// Wire format from the agent-hub MCP tools:
//
// - ``get_messages`` returns a JSON array; each entry has the fields
//   ``id``, ``from``, ``to``, ``message``, ``timestamp``. We rename
//   ``from`` → ``sender`` and ``message`` → ``body`` because ``from``
//   is a reserved word in some TS contexts and ``message`` collides
//   with the standard ``Error.message`` slot.
// - ``get_participants`` returns a mixed JSON array of
//   ``type === "person"`` and ``type === "team"``. The SDK exposes only
//   the ``person`` entries; team surface is deferred to a later
//   milestone.
//
// Parsers are deliberately lenient — server-side schema drift drops
// individual rows instead of crashing the whole batch.

/** One unread message fetched via ``getUnread``. */
export interface IncomingMessage {
  /** Server-assigned UUID; pass back to ``ack`` to mark as read. */
  readonly id: string;
  /** The ``@handle`` that sent the message. */
  readonly sender: string;
  /** The recipient (``@self`` for DMs, ``@team`` for team broadcasts). */
  readonly to: string;
  /** The human-readable message body. */
  readonly body: string;
  /** ISO-8601 UTC timestamp string from the server. */
  readonly timestamp: string;
}

/** One ``person``-type participant in ``getParticipants``. */
export interface Participant {
  /** The participant's ``@handle``. */
  readonly name: string;
  /** Optional role descriptor. ``null`` if registered without one. */
  readonly displayName: string | null;
  /**
   * Worker mode the participant declared at register-time
   * (``stateful`` / ``stateless`` / ``global``). ``null`` if absent.
   */
  readonly mode: string | null;
  /**
   * Whether the participant currently has an inbox subscription open.
   * Useful for filtering "who's actually reachable right now".
   */
  readonly isOnline: boolean;
}

/**
 * Parse the JSON body of ``get_messages`` into typed records.
 *
 * @param text the JSON text from the tool result.
 * @throws Error if the top-level shape is not a JSON array.
 */
export function parseMessages(text: string): IncomingMessage[] {
  const data = JSON.parse(text) as unknown;
  if (!Array.isArray(data)) {
    throw new Error(`Unexpected get_messages response: ${JSON.stringify(data)}`);
  }
  const result: IncomingMessage[] = [];
  for (const row of data) {
    if (typeof row !== "object" || row === null) {
      continue;
    }
    const raw = row as Record<string, unknown>;
    // Required keys — if any are missing or non-string, skip rather
    // than crash the whole batch (= defensive against schema drift).
    const id = raw.id;
    const from = raw.from;
    const to = raw.to;
    const message = raw.message;
    const timestamp = raw.timestamp;
    if (
      typeof id !== "string" ||
      typeof from !== "string" ||
      typeof to !== "string" ||
      typeof message !== "string" ||
      typeof timestamp !== "string"
    ) {
      continue;
    }
    result.push({
      id,
      sender: from,
      to,
      body: message,
      timestamp,
    });
  }
  return result;
}

/**
 * Parse the JSON body of ``get_participants``, keeping only persons.
 *
 * Team entries (``type === "team"``) are filtered; team surface is
 * deferred to a later milestone. Malformed entries are silently
 * dropped so one bad row doesn't kill the whole list.
 */
export function parseParticipants(text: string): Participant[] {
  const data = JSON.parse(text) as unknown;
  if (!Array.isArray(data)) {
    throw new Error(
      `Unexpected get_participants response: ${JSON.stringify(data)}`,
    );
  }
  const result: Participant[] = [];
  for (const row of data) {
    if (typeof row !== "object" || row === null) {
      continue;
    }
    const raw = row as Record<string, unknown>;
    if (raw.type !== "person") {
      continue;
    }
    const name = raw.name;
    if (typeof name !== "string" || name.length === 0) {
      continue;
    }
    const displayNameRaw = raw.display_name;
    const modeRaw = raw.mode;
    result.push({
      name,
      displayName: typeof displayNameRaw === "string" ? displayNameRaw : null,
      mode: typeof modeRaw === "string" ? modeRaw : null,
      isOnline: Boolean(raw.is_online ?? false),
    });
  }
  return result;
}
