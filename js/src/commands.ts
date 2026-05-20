// Command routing for ``/<verb>`` messages. Mirrors Python's
// ``agent_hub_sdk/commands.py`` 1:1, with TS-idiomatic registration
// (functional rather than decorator-based since TS class decorators are
// still stage-3).
//
// See ``docs/design.md`` and ``agent-hub#92`` for the cross-bridge
// command convention. Built-ins (``/ping`` → ``pong``, ``/status``,
// ``/help``) match the Python side exactly.

import type { IncomingMessage } from "./messages.js";
import type { HubSession } from "./session.js";

/**
 * Handler signature: ``async (msg, hub, args) => string | null``.
 *
 * - ``msg`` — the full ``IncomingMessage``.
 * - ``hub`` — the live ``HubSession`` (= handlers can ``send``,
 *   ``getParticipants``, etc. mid-handler).
 * - ``args`` — text after the command token, ``trim``ed. Empty string
 *   when no args. Handlers parse this themselves.
 * - return ``string`` → SDK sends as reply to ``msg.sender``, then acks.
 * - return ``null`` → SDK does **not** auto-reply; SDK still acks.
 * - throw → SDK logs + sends a generic warning reply, then acks. The
 *   exception does **not** propagate to the inbox iterator.
 */
export type CommandHandler = (
  msg: IncomingMessage,
  hub: HubSession,
  args: string,
) => Promise<string | null>;

/**
 * Result of ``CommandRouter.dispatch``. ``"handled"`` means the
 * router handled the message (= inbox iterator should NOT yield).
 * ``"yield"`` means the inbox iterator SHOULD yield (= passthrough,
 * non-command, or ``unknown: "yield"`` mode).
 */
export type DispatchResult = "handled" | "yield";

// Plain-text reply for the built-in /ping. ``agent-hub-sdk#10`` Rev 2
// settled on plain text rather than the command-format ``/pong``.
const PING_REPLY = "pong";

const DEFAULT_REJECT_FORMAT = "command not found: {cmd}";

const BUILTIN_NAMES = ["/ping", "/status", "/help"] as const;
const BUILTIN_DESCRIPTIONS: Record<string, string> = {
  "/ping": "health check",
  "/status": "bridge state (idle/busy/rate-limited)",
  "/help": "show this help",
};

/** Default bridge status when ``HubSession.setStatus`` hasn't been called. */
export const DEFAULT_STATUS = "idle";

/**
 * Split a message body into ``[command, args]``.
 *
 * A command is the first whitespace-delimited token of a body whose
 * first non-whitespace character is ``/``. The bare ``/`` is not a
 * command (returns ``[null, ""]``).
 *
 * Examples (TSDoc):
 * ```
 * parseCommand("/list verbose") → ["/list", "verbose"]
 * parseCommand("  /ping  ")     → ["/ping", ""]
 * parseCommand("hello")          → [null, ""]
 * parseCommand("/")              → [null, ""]
 * parseCommand("")               → [null, ""]
 * parseCommand(null)             → [null, ""]
 * ```
 */
export function parseCommand(
  text: string | null | undefined,
): [string | null, string] {
  if (!text) {
    return [null, ""];
  }
  const stripped = text.trim();
  if (!stripped.startsWith("/")) {
    return [null, ""];
  }
  const spaceIdx = stripped.search(/\s/);
  let cmd: string;
  let args: string;
  if (spaceIdx === -1) {
    cmd = stripped;
    args = "";
  } else {
    cmd = stripped.slice(0, spaceIdx);
    args = stripped.slice(spaceIdx + 1).trim();
  }
  if (cmd === "/") {
    return [null, ""];
  }
  return [cmd, args];
}

/** Options to ``new CommandRouter()``. */
export interface CommandRouterOptions {
  /** Register built-in handlers for ``/ping`` ``/status`` ``/help``. */
  builtins?: boolean;
  /**
   * Behaviour for ``/foo`` with no handler and no passthrough.
   *
   * - ``"reject"`` (default) → SDK auto-replies with ``rejectFormat``
   *   and acks.
   * - ``"yield"`` → SDK yields the message to the consumer just like
   *   a non-command body.
   */
  unknown?: "yield" | "reject";
  /**
   * Format template for the auto-reject reply. Default is
   * ``"command not found: {cmd}"``. The ``{cmd}`` placeholder is
   * filled with the parsed command token (including leading slash).
   */
  rejectFormat?: string;
}

/** Options to ``router.command(...)`` and ``router.passthrough(...)``. */
export interface CommandOptions {
  description?: string;
}

/**
 * Dispatch table for ``/`` prefix command messages.
 *
 * Construct, register handlers / passthroughs, then drive
 * ``router.dispatch`` from your inbox loop. M4 ships the low-level
 * driver shape; a higher-level ``hub.inbox({ commands: router, ... })``
 * convenience that wraps push + poll + heartbeat + dispatch into one
 * call lands in a follow-up (see issue #18 Minor 2).
 *
 * ```ts
 * import { AgentHub, CommandRouter, type IncomingMessage } from "@kishibashi3/agent-hub-sdk";
 *
 * const router = new CommandRouter();
 *
 * router.command("/active", async (msg, hub, args) => {
 *   return `working on ${currentTask()}`;
 * }, { description: "current task" });
 *
 * router.passthrough("/explain", { description: "ask the LLM" });
 *
 * await using handle = await AgentHub.connect({
 *   user: "my-bridge",
 *   mcpClientFactory: createMyMcpClient,
 * });
 * const hub = handle.session;
 *
 * await hub.register();
 * await hub.subscribeInbox();
 *
 * for await (const _uri of hub.inboxPushes()) {
 *   const messages = await hub.getUnread();
 *   for (const msg of messages) {
 *     const result = await router.dispatch(msg, hub);
 *     if (result === "yield") {
 *       // /explain → reaches here (= passthrough)
 *       // /foo with `unknown: "yield"` → reaches here
 *       // natural language → reaches here
 *       await myHandler(msg);
 *       await hub.ack(msg.id);
 *     }
 *     // result === "handled" → router already sent reply + acked
 *   }
 * }
 * ```
 *
 * ``/ping`` / ``/status`` / ``/help`` are intercepted by the
 * built-in handlers; unknown ``/foo`` is auto-replied with
 * ``command not found: /foo`` by default (configurable via
 * ``unknown`` and ``rejectFormat``).
 */
export class CommandRouter {
  private readonly _handlers = new Map<string, CommandHandler>();
  private readonly _descriptions = new Map<string, string>();
  private readonly _passthrough = new Set<string>();
  private readonly _builtinsEnabled: boolean;
  private readonly _unknownMode: "yield" | "reject";
  private readonly _rejectFormat: string;

  constructor(options: CommandRouterOptions = {}) {
    this._builtinsEnabled = options.builtins ?? true;
    this._unknownMode = options.unknown ?? "reject";
    this._rejectFormat = options.rejectFormat ?? DEFAULT_REJECT_FORMAT;
  }

  /**
   * Register a handler for ``path``. ``path`` must start with ``/``.
   * Re-registering overwrites (= explicit override). Overriding a
   * built-in is supported — the user's handler wins.
   */
  command(
    path: string,
    handler: CommandHandler,
    options: CommandOptions = {},
  ): void {
    if (!path.startsWith("/")) {
      throw new Error(`command path must start with '/' (got ${path})`);
    }
    this._handlers.set(path, handler);
    if (options.description !== undefined) {
      this._descriptions.set(path, options.description);
    }
  }

  /**
   * Mark ``path`` as a known command that the SDK should yield (= LLM
   * answers it). ``path`` must start with ``/``.
   */
  passthrough(path: string, options: CommandOptions = {}): void {
    if (!path.startsWith("/")) {
      throw new Error(`passthrough path must start with '/' (got ${path})`);
    }
    this._passthrough.add(path);
    if (options.description !== undefined) {
      this._descriptions.set(path, options.description);
    }
  }

  /**
   * Route one inbox message. Returns whether to yield to the consumer.
   *
   * Precedence:
   *   1. Non-command body → "yield".
   *   2. User-registered handler → run it, return "handled".
   *   3. Built-in handler (if enabled and not overridden) → run it.
   *   4. Passthrough → "yield".
   *   5. Unknown — depends on ``unknown`` mode.
   *
   * For "handled" returns, the message is **also** acked. For "yield"
   * returns, the consumer is responsible for acking.
   */
  async dispatch(msg: IncomingMessage, hub: HubSession): Promise<DispatchResult> {
    const [cmd, args] = parseCommand(msg.body);
    if (cmd === null) {
      return "yield";
    }

    // 1. User handler wins
    const handler = this._handlers.get(cmd);
    if (handler !== undefined) {
      await this._runHandler(handler, msg, hub, cmd, args);
      return "handled";
    }

    // 2. Built-in handler (no user override)
    if (this._builtinsEnabled) {
      const builtin = BUILTIN_HANDLERS[cmd];
      if (builtin !== undefined) {
        await this._runBuiltin(builtin, msg, hub, cmd, args);
        return "handled";
      }
    }

    // 3. Passthrough — known command, defer to consumer
    if (this._passthrough.has(cmd)) {
      return "yield";
    }

    // 4. Unknown
    if (this._unknownMode === "reject") {
      await this._rejectUnknown(msg, hub, cmd);
      return "handled";
    }
    return "yield";
  }

  private async _runHandler(
    handler: CommandHandler,
    msg: IncomingMessage,
    hub: HubSession,
    cmd: string,
    args: string,
  ): Promise<void> {
    try {
      const result = await handler(msg, hub, args);
      if (typeof result === "string") {
        await hub.send(msg.sender, result).catch((err) => {
          console.error(`could not send reply for ${cmd}:`, err);
        });
      }
    } catch (err) {
      console.error(`command ${cmd} handler failed:`, err);
      await hub
        .send(
          msg.sender,
          `:warning: command ${cmd} failed (see bridge log)`,
        )
        .catch((sendErr) => {
          console.error(`could not send error reply for ${cmd}:`, sendErr);
        });
    }
    await hub.ack(msg.id).catch((err) => {
      console.error(`could not ack ${msg.id} after handler:`, err);
    });
  }

  private async _runBuiltin(
    builtin: BuiltinHandler,
    msg: IncomingMessage,
    hub: HubSession,
    cmd: string,
    args: string,
  ): Promise<void> {
    try {
      const result = await builtin(this, msg, hub, args);
      if (typeof result === "string") {
        await hub.send(msg.sender, result).catch((err) => {
          console.error(`could not send reply for built-in ${cmd}:`, err);
        });
      }
    } catch (err) {
      console.error(`built-in ${cmd} failed:`, err);
      await hub
        .send(
          msg.sender,
          `:warning: built-in ${cmd} failed (see bridge log)`,
        )
        .catch((sendErr) => {
          console.error(`could not send error reply for built-in ${cmd}:`, sendErr);
        });
    }
    await hub.ack(msg.id).catch((err) => {
      console.error(`could not ack ${msg.id} after built-in:`, err);
    });
  }

  private async _rejectUnknown(
    msg: IncomingMessage,
    hub: HubSession,
    cmd: string,
  ): Promise<void> {
    console.info(`[command] from=${msg.sender} unknown=${cmd}`);
    const reply = this._rejectFormat.replace("{cmd}", cmd);
    await hub.send(msg.sender, reply).catch((err) => {
      console.error(`could not send reject reply for ${cmd}:`, err);
    });
    await hub.ack(msg.id).catch((err) => {
      console.error(`could not ack ${msg.id} after reject:`, err);
    });
  }

  /**
   * Generate the ``/help`` reply text from registered handlers +
   * passthroughs + built-ins. Internal — public-ish for tests.
   */
  _generateHelpText(): string {
    const lines: string[] = [];
    const emitted = new Set<string>();

    if (this._builtinsEnabled) {
      for (const name of BUILTIN_NAMES) {
        if (emitted.has(name)) continue;
        // User override takes precedence — listed in the next loop.
        if (this._handlers.has(name)) continue;
        const desc =
          this._descriptions.get(name) ?? BUILTIN_DESCRIPTIONS[name] ?? "";
        lines.push(`${name.padEnd(12)} — ${desc}`);
        emitted.add(name);
      }
    }

    // User handlers, in registration (Map insertion) order.
    for (const name of this._handlers.keys()) {
      if (emitted.has(name)) continue;
      const desc =
        this._descriptions.get(name) ?? BUILTIN_DESCRIPTIONS[name] ?? "";
      lines.push(`${name.padEnd(12)} — ${desc}`);
      emitted.add(name);
    }

    // Passthroughs, alphabetical for determinism.
    const passthroughSorted = Array.from(this._passthrough).sort();
    for (const name of passthroughSorted) {
      if (emitted.has(name)) continue;
      const desc = this._descriptions.get(name) ?? "";
      lines.push(`${name.padEnd(12)} — ${desc}`);
      emitted.add(name);
    }

    if (lines.length === 0) {
      return "(no commands registered)";
    }
    return lines.join("\n");
  }
}

// ---------------------------------------------------------------------
// Built-in handler implementations
// ---------------------------------------------------------------------

type BuiltinHandler = (
  router: CommandRouter,
  msg: IncomingMessage,
  hub: HubSession,
  args: string,
) => Promise<string | null>;

const builtinPing: BuiltinHandler = async (_router, msg, _hub, _args) => {
  console.info(`[command] /ping → pong (from=${msg.sender})`);
  return PING_REPLY;
};

const builtinStatus: BuiltinHandler = async (_router, _msg, hub, _args) => {
  // ``HubSession.setStatus`` writes to the session's internal field;
  // the property accessor returns the latest value or "idle".
  return hub.status;
};

const builtinHelp: BuiltinHandler = async (router, _msg, _hub, _args) => {
  return router._generateHelpText();
};

const BUILTIN_HANDLERS: Record<string, BuiltinHandler> = {
  "/ping": builtinPing,
  "/status": builtinStatus,
  "/help": builtinHelp,
};
