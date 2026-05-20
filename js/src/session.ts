// MCP session lifecycle for the ``stateful`` mode. Mirrors Python's
// ``agent_hub_sdk/session.py``.
//
// ``HubSession`` owns one MCP client over streamable HTTP plus a queue
// of inbox-push URIs. It's the layer that talks the MCP protocol;
// ``AgentHub.connect`` wraps it with the ergonomic public API.

import type { CommandRouter } from "./commands.js";
import type { Config } from "./config.js";
import { HubTransientError } from "./errors.js";
import type { IncomingMessage, Participant } from "./messages.js";
import { parseMessages, parseParticipants } from "./messages.js";
import type { ToolResult } from "./transport.js";
import { raiseForSendError, raiseForToolError } from "./transport.js";

// Default safety-net poll interval in ms (= 30s). Mirrors Python's
// ``_DEFAULT_INBOX_POLL_INTERVAL_S``. Overridable via env so deploys
// with different RTT / load can tune without a code change.
const DEFAULT_INBOX_POLL_INTERVAL_MS = 30_000;
const INBOX_POLL_INTERVAL_ENV = "AGENT_HUB_INBOX_POLL_INTERVAL_S";

const DEFAULT_HEARTBEAT_INTERVAL_MS = 60_000;
const INBOX_QUEUE_CAPACITY = 100;
const INBOX_OUTPUT_CAPACITY = 1024;

// Plain-text ``/ping`` reply for the legacy ``autoPong`` path. Matches
// Python's ``_PONG_REPLY`` after the M2.1 plain-text change.
const PING_REPLY = "pong";
const PING_TOKEN = "/ping";

/**
 * Generic MCP client interface that the SDK works against. The real
 * implementation comes from ``@modelcontextprotocol/sdk``'s streamable
 * HTTP client; tests can plug in a stub. Keeping this interface narrow
 * (= just the calls the SDK actually needs) decouples the SDK from
 * MCP SDK minor-version churn.
 */
export interface McpClient {
  initialize(): Promise<void>;
  close(): Promise<void>;
  callTool(
    name: string,
    args: Record<string, unknown>,
  ): Promise<ToolResult>;
  subscribeResource(uri: string): Promise<void>;
  listTools(): Promise<unknown>;
  /** Register a notification handler called on every server notification. */
  setNotificationHandler(
    handler: (notification: McpNotification) => void,
  ): void;
}

/** Minimal shape of MCP notifications the SDK cares about. */
export interface McpNotification {
  readonly method?: string;
  readonly params?: {
    readonly uri?: string;
    [k: string]: unknown;
  };
}

/**
 * Factory that builds an ``McpClient`` for a given ``Config``. The
 * default factory (= production) opens a streamable-HTTP MCP client;
 * tests pass a stub-returning factory instead.
 */
export type McpClientFactory = (config: Config) => Promise<McpClient>;

/** Options to ``HubSession.inbox``. */
export interface InboxOptions {
  /** ``CommandRouter`` to route ``/`` commands; ``null`` disables routing. */
  commands?: CommandRouter | null;
  /** Legacy M2 ``/ping`` interception when ``commands`` is null. */
  autoPong?: boolean;
  /** Safety-net poll interval in ms; ``null`` uses env or 30000. */
  pollIntervalMs?: number | null;
  /** Liveness probe interval in ms. */
  heartbeatIntervalMs?: number;
}

/** Options to ``hub.sendWithRetry``. */
export interface SendWithRetryOptions {
  maxAttempts?: number;
  baseDelayMs?: number;
  sleepFn?: (delayMs: number) => Promise<void>;
}

/** A ``HubSession`` that can be ``await using``'d for automatic cleanup. */
export interface HubSessionHandle extends AsyncDisposable {
  readonly session: HubSession;
}

/**
 * One MCP session against agent-hub, plus an inbox-push queue.
 *
 * Don't construct directly — ``AgentHub.connect`` wraps this.
 */
export class HubSession {
  /** Latest status string returned by the built-in ``/status``. */
  status: string = "idle";

  /** Bounded queue of inbox-push URIs fed by the notification handler. */
  private readonly _pushQueue: string[] = [];
  /** Resolvers waiting for the next push event. */
  private readonly _pushWaiters: Array<(uri: string) => void> = [];
  /** Closed flag — once true, ``inboxPushes`` returns done. */
  private _closed = false;

  constructor(
    /** Underlying MCP client (= mock or real). */
    public readonly client: McpClient,
    /** Resolved config (= immutable for the session lifetime). */
    public readonly config: Config,
  ) {
    // Wire up notification handler so push events land in the queue.
    client.setNotificationHandler((notification) => {
      if (
        notification.method === "notifications/resources/updated" &&
        notification.params?.uri !== undefined
      ) {
        this._enqueuePush(notification.params.uri);
      }
    });
  }

  /**
   * Open a fresh ``HubSession`` and return a disposable handle. The
   * client is initialised before yielding. Use ``await using`` to
   * guarantee cleanup, mirroring Python's ``async with``.
   */
  static async open(
    config: Config,
    factory: McpClientFactory,
  ): Promise<HubSessionHandle> {
    const client = await factory(config);
    await client.initialize();
    const session = new HubSession(client, config);
    return {
      session,
      async [Symbol.asyncDispose]() {
        session._closed = true;
        // Wake any pending push iterator so it can return.
        for (const waiter of session._pushWaiters.splice(0)) {
          waiter("");
        }
        await client.close();
      },
    };
  }

  /** Update the value returned by ``/status``. */
  setStatus(value: string): void {
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(
        `setStatus: value must be a non-empty string (got ${JSON.stringify(value)})`,
      );
    }
    this.status = value;
  }

  // ------------------------------------------------------------------
  // Registration / presence
  // ------------------------------------------------------------------

  async register(): Promise<string> {
    const displayName = this.config.displayName ?? this.config.user;
    const result = await this.client.callTool("register", {
      name: this.config.user,
      display_name: displayName,
      mode: this.config.mode,
    });
    return raiseForToolError(result, "register");
  }

  // ------------------------------------------------------------------
  // Send
  // ------------------------------------------------------------------

  async send(to: string, message: string): Promise<string> {
    let result: ToolResult;
    try {
      result = await this.client.callTool("send_message", { to, message });
    } catch (err) {
      throw new HubTransientError(
        `send to ${to} transport failure: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    return raiseForSendError(result, to);
  }

  async sendWithRetry(
    to: string,
    message: string,
    options: SendWithRetryOptions = {},
  ): Promise<string> {
    const maxAttempts = options.maxAttempts ?? 3;
    const baseDelayMs = options.baseDelayMs ?? 1000;
    const sleep = options.sleepFn ?? defaultSleep;

    let lastTransient: HubTransientError | undefined;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      try {
        return await this.send(to, message);
      } catch (err) {
        if (err instanceof HubTransientError) {
          lastTransient = err;
          if (attempt >= maxAttempts - 1) {
            throw err;
          }
          const delayMs = baseDelayMs * Math.pow(2, attempt);
          console.warn(
            `send to ${to} transient (attempt ${attempt + 1}/${maxAttempts}), sleeping ${delayMs}ms`,
          );
          await sleep(delayMs);
        } else {
          throw err;
        }
      }
    }
    // Unreachable — loop always returns or throws.
    throw lastTransient ?? new Error("send_with_retry: unreachable");
  }

  // ------------------------------------------------------------------
  // Inbox: read / ack / subscribe / pushes
  // ------------------------------------------------------------------

  async getUnread(): Promise<IncomingMessage[]> {
    const result = await this.client.callTool("get_messages", {});
    const text = raiseForToolError(result, "get_messages");
    return parseMessages(text);
  }

  async ack(messageId: string): Promise<void> {
    const result = await this.client.callTool("mark_as_read", {
      message_id: messageId,
    });
    raiseForToolError(result, "mark_as_read");
  }

  async subscribeInbox(): Promise<void> {
    const inboxUri = `inbox://@${this.config.user}`;
    await this.client.subscribeResource(inboxUri);
  }

  /**
   * Async iterator over inbox-push URIs. Each yield is a coalescing
   * hint — call ``getUnread()`` to fetch the actual messages.
   *
   * Stops when the session is closed (= the underlying handle's
   * ``Symbol.asyncDispose`` fires).
   */
  async *inboxPushes(): AsyncGenerator<string, void, void> {
    while (!this._closed) {
      // Drain anything already queued without blocking.
      while (this._pushQueue.length > 0) {
        const uri = this._pushQueue.shift()!;
        yield uri;
      }
      if (this._closed) return;
      // Wait for the next push.
      const uri = await new Promise<string>((resolve) => {
        this._pushWaiters.push(resolve);
      });
      if (this._closed) return;
      yield uri;
    }
  }

  // ------------------------------------------------------------------
  // Other tools
  // ------------------------------------------------------------------

  async getParticipants(): Promise<Participant[]> {
    let result: ToolResult;
    try {
      result = await this.client.callTool("get_participants", {});
    } catch (err) {
      throw new HubTransientError(
        `get_participants transport failure: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    const text = raiseForToolError(result, "get_participants");
    return parseParticipants(text);
  }

  async heartbeat(): Promise<void> {
    await this.client.listTools();
  }

  // ------------------------------------------------------------------
  // Internal helpers
  // ------------------------------------------------------------------

  private _enqueuePush(uri: string): void {
    // Hand off to any waiting consumer first; otherwise buffer.
    const waiter = this._pushWaiters.shift();
    if (waiter !== undefined) {
      waiter(uri);
      return;
    }
    if (this._pushQueue.length >= INBOX_QUEUE_CAPACITY) {
      console.warn("inbox push queue overflow, dropping");
      return;
    }
    this._pushQueue.push(uri);
  }
}

/** Default ``setTimeout``-based sleep used by ``sendWithRetry``. */
function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Re-export constants so tests + bridges can reference the same values.
export {
  DEFAULT_INBOX_POLL_INTERVAL_MS,
  DEFAULT_HEARTBEAT_INTERVAL_MS,
  INBOX_POLL_INTERVAL_ENV,
  INBOX_OUTPUT_CAPACITY,
  PING_REPLY,
  PING_TOKEN,
};
