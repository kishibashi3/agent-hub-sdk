// Low-level MCP tool-call helpers shared by the session layer. Mirrors
// Python's ``agent_hub_sdk/transport.py`` 1:1.
//
// These functions extract content from MCP ``CallToolResult`` records
// and route ``isError=true`` responses through ``classifyHubError`` so
// callers deal in ``PeerNotFoundError`` / ``HubTransientError`` instead
// of inspecting tool-result blobs by hand.

import {
  HubTransientError,
  PeerNotFoundError,
  classifyHubError,
} from "./errors.js";

// Loosely-typed shape of an MCP ``CallToolResult``. The full SDK type
// includes a wider content-block union; we narrow to what we actually
// inspect (= text blocks + isError) so the helper signatures stay
// stable across MCP SDK minor versions.
export interface ToolContentText {
  readonly type: "text";
  readonly text: string;
}
export type ToolContentBlock = ToolContentText | { readonly type: string };

export interface ToolResult {
  readonly content?: ReadonlyArray<ToolContentBlock> | null;
  readonly isError?: boolean | null;
}

/**
 * Concatenate the text content of an MCP tool result.
 *
 * Non-text blocks (images, resources) are silently ignored — the
 * agent-hub tools we use only return text today, so this is a
 * deliberate shortcut.
 */
export function extractText(
  content: ReadonlyArray<ToolContentBlock> | null | undefined,
): string {
  if (!content) {
    return "";
  }
  const parts: string[] = [];
  for (const block of content) {
    if (block.type === "text" && typeof (block as ToolContentText).text === "string") {
      parts.push((block as ToolContentText).text);
    }
  }
  return parts.join("\n");
}

/**
 * Throw the right error class for an MCP ``ToolResult``.
 *
 * On success returns the joined text content. On ``isError=true`` the
 * error text is run through ``classifyHubError``; transient errors
 * become ``HubTransientError``, anything else becomes a generic
 * ``Error`` (the caller decides whether to special-case it).
 *
 * Peer-not-found is **not** raised here — only ``send`` cares about
 * that distinction. Use ``raiseForSendError`` from the send path.
 */
export function raiseForToolError(result: ToolResult, op: string): string {
  const text = extractText(result.content);
  if (!result.isError) {
    return text;
  }
  const kind = classifyHubError(text);
  if (kind === "transient") {
    throw new HubTransientError(`${op} transient: ${text || "(no detail)"}`);
  }
  throw new Error(`${op} failed: ${text || "(no detail)"}`);
}

/**
 * ``send``-specific variant that also surfaces peer-not-found.
 *
 * Same shape as ``raiseForToolError`` but classifies ``isError``
 * payloads into three buckets:
 *
 * - peer_not_found → ``PeerNotFoundError``
 * - transient → ``HubTransientError``
 * - anything else → ``Error``
 */
export function raiseForSendError(result: ToolResult, to: string): string {
  const text = extractText(result.content);
  if (!result.isError) {
    return text;
  }
  const kind = classifyHubError(text);
  if (kind === "peer_not_found") {
    throw new PeerNotFoundError(to, text || "(no detail)");
  }
  if (kind === "transient") {
    throw new HubTransientError(`send to ${to} transient: ${text || "(no detail)"}`);
  }
  throw new Error(`send to ${to} failed: ${text || "(no detail)"}`);
}
