// High-level ``AgentHub`` facade. Mirrors Python's
// ``agent_hub_sdk/client.py`` — the main entry point for SDK
// consumers.
//
// M5 (issue #27): ``connect`` now auto-registers the consumer with
// the hub before returning the handle. Caller code no longer needs an
// explicit ``await hub.session.register()`` line at startup:
//
// ```ts
// await using hub = await AgentHub.connect({ user: "my-bridge" });
// // `register` already ran — go straight to work.
// await hub.session.send("@peer", "hello");
// ```
//
// If auto-register fails (network error, server unavailable, etc.) the
// just-opened session is torn down before the exception propagates so
// the caller doesn't end up with a half-open handle. Subsequent calls
// to ``hub.session.register(...)`` remain supported and pass through
// to the server (= idempotent duplicate or explicit display-name
// update).

import type { Config, Mode, ResolveConfigOptions } from "./config.js";
import { resolveConfig } from "./config.js";
import { HubSession, type HubSessionHandle, type McpClientFactory } from "./session.js";

/** Options to ``AgentHub.connect``. */
export interface ConnectOptions {
  user: string;
  mode?: Mode;
  tenant?: string | null;
  displayName?: string | null;
  url?: string | null;
  pat?: string | null;
  /** Environment-variable lookup; defaults to ``process.env``. */
  env?: Record<string, string | undefined>;
  /**
   * MCP client factory. The default factory uses
   * ``@modelcontextprotocol/sdk``'s streamable-HTTP client. Tests pass
   * a stub-returning factory; bridges may override for custom
   * transports.
   */
  mcpClientFactory?: McpClientFactory;
}

/**
 * Static façade. Call ``AgentHub.connect(...)``, don't instantiate.
 *
 * ``connect`` returns a disposable handle whose ``session`` property
 * carries the live ``HubSession``. Use ``await using`` to guarantee
 * cleanup:
 *
 * ```ts
 * await using hub = await AgentHub.connect({ user: "my-bridge" });
 * await hub.session.register();
 * // ...
 * // Implicit cleanup at scope exit.
 * ```
 */
export class AgentHub {
  /**
   * Open an agent-hub session and auto-register the consumer.
   *
   * Resolves config, opens the MCP session via ``HubSession.open``,
   * then calls ``hub.session.register()`` automatically before
   * returning the handle (M5, issue #27). Peers see this consumer in
   * ``get_participants`` immediately without the bridge needing a
   * startup-time ``register()`` call.
   *
   * @throws ConfigurationError if ``url`` or ``pat`` cannot be
   *   resolved from args + environment. **No implicit default URL** —
   *   the SDK refuses to start rather than silently connecting to a
   *   wrong endpoint.
   * @throws Error if the auto-register call fails (e.g. the server is
   *   unreachable or rejects the registration). The just-opened MCP
   *   session is closed before the rejection propagates so the caller
   *   never sees a half-open handle.
   *
   * After ``connect``, calls to ``hub.session.register(...)`` remain
   * supported and pass through to the server — the server treats
   * duplicates idempotently (= harmless re-confirm, or an explicit
   * display-name update if the field changed).
   */
  static async connect(options: ConnectOptions): Promise<HubSessionHandle> {
    const config = resolveConfig(options as ResolveConfigOptions);
    const factory =
      options.mcpClientFactory ??
      ((c: Config) => Promise.reject<never>(noDefaultFactoryError(c)));
    const handle = await HubSession.open(config, factory);
    try {
      await handle.session.register();
    } catch (err) {
      // Auto-register failed (network error, server unavailable, etc.).
      // Tear down the MCP session we just opened so the caller doesn't
      // end up with a half-open handle leaking transports. Same
      // fail-fast spirit as ``ConfigurationError`` (redline #1).
      await handle[Symbol.asyncDispose]();
      throw err;
    }
    return handle;
  }
}

function noDefaultFactoryError(_config: Config): Error {
  return new Error(
    "AgentHub.connect: no MCP client factory configured. Pass " +
      "`mcpClientFactory: createStreamableHttpFactory(...)` from " +
      "`@kishibashi3/agent-hub-sdk/streamable-http` " +
      "(or supply your own factory for tests). " +
      "Default factory wiring lands once the @modelcontextprotocol/sdk " +
      "Node integration shape is finalised for this SDK; see issue #18.",
  );
}
