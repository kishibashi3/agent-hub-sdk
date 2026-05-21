// MCP session lifecycle for the ``stateful`` mode. Mirrors Python's
// ``agent_hub_sdk/session.py`` (M2 inbox iterator + M3 oneShot).
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
  /**
   * Factory stored by ``HubSession.open`` so ``oneShot()`` can open a
   * sibling session using the same transport. Undefined when the session
   * was constructed directly (e.g. in tests that bypass ``open``).
   */
  _factory?: McpClientFactory;

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
    session._factory = factory; // Stored so oneShot() can open a sibling session.
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
  // One-shot session (M3) — fresh MCP session for one batch of ops
  // ------------------------------------------------------------------

  /**
   * Open a fresh ``HubSession`` for one batch of tool calls.
   *
   * Returns a new ``HubSessionHandle`` whose ``session`` is bound to its
   * own independent MCP transport and client. The outer session (``this``)
   * is not touched — its long-lived SSE subscribe, push queue, and
   * ``status`` are preserved.
   *
   * Use ``await using`` to guarantee cleanup:
   *
   * ```ts
   * await using inner = await hub.session.oneShot();
   * for (const msg of await inner.session.getUnread()) {
   *   await inner.session.send(msg.sender, await llm(msg.body));
   *   await inner.session.ack(msg.id);
   * }
   * // inner scope exits → fresh session closed.
   * ```
   *
   * **Independence**: the returned session has its own MCP client,
   * push queue, and status (starts at ``"idle"``). Only the immutable
   * ``Config`` (URL, PAT, tenant, user, mode) is shared.
   *
   * **Mode-agnostic**: available on any session regardless of ``mode``.
   *
   * Mirrors Python's ``HubSession.one_shot()`` (M3).
   *
   * @throws Error if the session was not opened via ``HubSession.open``
   *   (= no factory available).
   */
  async oneShot(): Promise<HubSessionHandle> {
    if (this._factory === undefined) {
      throw new Error(
        "HubSession.oneShot: no factory available — session was not " +
          "opened via HubSession.open (= AgentHub.connect).",
      );
    }
    return HubSession.open(this.config, this._factory);
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
  // Inbox iterator (M2) — push + poll + heartbeat + command routing
  // ------------------------------------------------------------------

  /**
   * Async generator over inbox messages. Merges three concurrent
   * producers into a single ``for await`` loop:
   *
   * 1. **SSE push** — low-latency path; drains on every server
   *    notification.
   * 2. **Safety-net poll** — drains every ``pollIntervalMs`` ms
   *    (default 30s, override via ``AGENT_HUB_INBOX_POLL_INTERVAL_S``
   *    env or ``options.pollIntervalMs``) in case the SSE stream goes
   *    silent.
   * 3. **Heartbeat** — calls ``listTools`` every
   *    ``heartbeatIntervalMs`` ms (default 60s) as a liveness probe.
   *    A dead session surfaces as a thrown error to the caller's
   *    reconnect loop.
   *
   * Commands (``/ping``, ``/status``, etc.) are intercepted before
   * reaching the consumer. Pass a ``CommandRouter`` via
   * ``options.commands`` for full command dispatch; leave it unset to
   * use the legacy ``autoPong`` mode (default ``true`` = intercept
   * ``/ping`` only).
   *
   * In-flight dedup: each message ID is tracked for the lifetime of
   * the iterator so SSE replay double-pushes never double-yield the
   * same message (mirrors Python issue #31 fix).
   *
   * Usage:
   * ```ts
   * for await (const msg of hub.session.inbox({ commands: router })) {
   *   await processMessage(msg);
   *   await hub.session.ack(msg.id);
   * }
   * ```
   *
   * Mirrors Python's ``HubSession.inbox()`` (M2).
   */
  async *inbox(
    options: InboxOptions = {},
  ): AsyncGenerator<IncomingMessage, void, void> {
    // -- Resolve options -----------------------------------------------
    const resolvedPollMs: number = (() => {
      if (options.pollIntervalMs != null) return options.pollIntervalMs;
      const envVal = process.env[INBOX_POLL_INTERVAL_ENV];
      if (envVal !== undefined) {
        const parsed = parseFloat(envVal);
        if (isNaN(parsed)) {
          throw new Error(
            `${INBOX_POLL_INTERVAL_ENV}=${JSON.stringify(envVal)} is not a valid number`,
          );
        }
        return parsed * 1_000;
      }
      return DEFAULT_INBOX_POLL_INTERVAL_MS;
    })();
    const heartbeatMs =
      options.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS;
    const autoPong = options.autoPong ?? true;
    const commandRouter = options.commands ?? null;

    // Resolve the drain function once.
    const drain =
      commandRouter !== null
        ? (): Promise<IncomingMessage[]> =>
            this._drainWithRouter(commandRouter)
        : (): Promise<IncomingMessage[]> =>
            this._drainInterceptingPing(autoPong);

    // -- Output channel ------------------------------------------------
    // In-flight dedup: prevent SSE replay from double-yielding the same
    // message (mirrors Python's ``in_flight_ids`` set, issue #31).
    const inFlightIds = new Set<string>();

    type QueueItem =
      | { kind: "msg"; msg: IncomingMessage }
      | { kind: "error"; err: unknown };

    const outBuf: QueueItem[] = [];
    let outResolve: ((item: QueueItem) => void) | undefined;

    function emit(item: QueueItem): void {
      if (outResolve !== undefined) {
        const r = outResolve;
        outResolve = undefined;
        r(item);
      } else {
        outBuf.push(item);
      }
    }

    function enqueue(msg: IncomingMessage, source: string): void {
      if (inFlightIds.has(msg.id)) {
        return; // Already in-flight — dedup.
      }
      inFlightIds.add(msg.id);
      // Overflow guard: don't grow the buffer past capacity.
      const msgCount = outBuf.filter((i) => i.kind === "msg").length;
      if (msgCount >= INBOX_OUTPUT_CAPACITY) {
        inFlightIds.delete(msg.id);
        console.warn(`inbox output queue overflow (${source}), dropping`);
        return;
      }
      emit({ kind: "msg", msg });
    }

    function signalError(err: unknown): void {
      emit({ kind: "error", err });
    }

    // -- Cancellation --------------------------------------------------
    // AbortController: abort() is called when the consumer exits the
    // iterator (break / return / throw). All producer loops check the
    // signal and exit cleanly.
    const abort = new AbortController();
    const sig = abort.signal;

    /** Sleep for ``ms`` ms, resolve false immediately if aborted. */
    const sleepOrAbort = (ms: number): Promise<boolean> => {
      return new Promise<boolean>((resolve) => {
        if (sig.aborted) {
          resolve(false);
          return;
        }
        const t = setTimeout(() => resolve(true), ms);
        sig.addEventListener(
          "abort",
          () => {
            clearTimeout(t);
            resolve(false);
          },
          { once: true },
        );
      });
    };

    /**
     * Wait for the next inbox push URI, or ``null`` if aborted /
     * session closed. Uses the same ``_pushWaiters`` mechanism as
     * ``inboxPushes()``, but with an abort escape hatch.
     */
    const waitForPush = (): Promise<string | null> => {
      return new Promise<string | null>((resolve) => {
        if (sig.aborted || this._closed) {
          resolve(null);
          return;
        }
        // Drain already-buffered push (notification arrived before we
        // started waiting).
        if (this._pushQueue.length > 0) {
          resolve(this._pushQueue.shift()!);
          return;
        }
        let settled = false;
        const onPush = (uri: string): void => {
          if (settled) return;
          settled = true;
          resolve(uri === "" ? null : uri); // "" = session closed signal
        };
        this._pushWaiters.push(onPush);
        sig.addEventListener(
          "abort",
          () => {
            if (settled) return;
            settled = true;
            // Remove our resolver so _enqueuePush doesn't call it later.
            const idx = this._pushWaiters.indexOf(onPush);
            if (idx !== -1) this._pushWaiters.splice(idx, 1);
            resolve(null);
          },
          { once: true },
        );
      });
    };

    // -- Startup -------------------------------------------------------
    await this.subscribeInbox();

    // Pick up anything that arrived before the subscription took effect.
    for (const msg of await drain()) {
      enqueue(msg, "startup");
    }

    // -- Producers -----------------------------------------------------
    const pushLoop = async (): Promise<void> => {
      for (;;) {
        const uri = await waitForPush();
        if (uri === null) return;
        try {
          for (const msg of await drain()) enqueue(msg, "push");
        } catch (err) {
          signalError(err);
          return;
        }
      }
    };

    const pollLoop = async (): Promise<void> => {
      for (;;) {
        const ok = await sleepOrAbort(resolvedPollMs);
        if (!ok) return;
        try {
          for (const msg of await drain()) enqueue(msg, "poll");
        } catch (err) {
          signalError(err);
          return;
        }
      }
    };

    const heartbeatLoop = async (): Promise<void> => {
      for (;;) {
        const ok = await sleepOrAbort(heartbeatMs);
        if (!ok) return;
        try {
          await this.heartbeat();
        } catch (err) {
          signalError(err);
          return;
        }
      }
    };

    const allDone = Promise.allSettled([
      pushLoop(),
      pollLoop(),
      heartbeatLoop(),
    ]);

    // -- Consumer loop -------------------------------------------------
    try {
      for (;;) {
        let item: QueueItem;
        if (outBuf.length > 0) {
          item = outBuf.shift()!;
        } else {
          item = await new Promise<QueueItem>((resolve) => {
            outResolve = resolve;
          });
        }
        if (item.kind === "error") throw item.err;
        yield item.msg;
      }
    } finally {
      // Consumer exited (break / return / thrown error). Signal all
      // producers to stop, then wait for them to drain.
      abort.abort();
      await allDone;
    }
  }

  // ------------------------------------------------------------------
  // Private drain helpers
  // ------------------------------------------------------------------

  /**
   * Fetch unread messages and intercept ``/ping`` when ``autoPong`` is
   * true. Mirrors Python's ``_drain_intercepting_ping``.
   */
  private async _drainInterceptingPing(
    autoPong: boolean,
  ): Promise<IncomingMessage[]> {
    const messages = await this.getUnread();
    if (!autoPong) return messages;
    const remaining: IncomingMessage[] = [];
    for (const msg of messages) {
      if (msg.body.trim() !== PING_TOKEN) {
        remaining.push(msg);
        continue;
      }
      console.info(`[ping] from=${msg.sender} → pong`);
      try {
        await this.send(msg.sender, PING_REPLY);
      } catch (e) {
        console.error(`auto-pong send to ${msg.sender} failed:`, e);
      }
      try {
        await this.ack(msg.id);
      } catch (e) {
        console.error(`auto-pong ack for ${msg.id} failed:`, e);
      }
    }
    return remaining;
  }

  /**
   * Fetch unread messages and route each through ``router``. Messages
   * where ``dispatch`` returns ``"yield"`` are returned to the consumer;
   * ``"handled"`` messages (already replied + acked by the router) are
   * dropped. Mirrors Python's ``_drain_with_router``.
   */
  private async _drainWithRouter(
    router: CommandRouter,
  ): Promise<IncomingMessage[]> {
    const messages = await this.getUnread();
    const yielded: IncomingMessage[] = [];
    for (const msg of messages) {
      let result: Awaited<ReturnType<CommandRouter["dispatch"]>>;
      try {
        result = await router.dispatch(msg, this);
      } catch (err) {
        // Router.dispatch is supposed to catch handler errors internally.
        // If something escapes, log and yield so the consumer sees it.
        console.error(
          `command router crashed on message ${msg.id}; yielding to consumer`,
          err,
        );
        yielded.push(msg);
        continue;
      }
      if (result === "yield") yielded.push(msg);
    }
    return yielded;
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
