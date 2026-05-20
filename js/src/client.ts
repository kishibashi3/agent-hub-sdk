// High-level ``AgentHub`` facade. Mirrors Python's
// ``agent_hub_sdk/client.py`` — the main entry point for SDK
// consumers.
//
// ```ts
// await using hub = await AgentHub.connect({ user: "my-bridge" });
// await hub.session.register();
// await hub.session.send("@peer", "hello");
// ```

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
   * Open an agent-hub session.
   *
   * @throws ConfigurationError if ``url`` or ``pat`` cannot be
   *   resolved from args + environment. **No implicit default URL** —
   *   the SDK refuses to start rather than silently connecting to a
   *   wrong endpoint.
   */
  static async connect(options: ConnectOptions): Promise<HubSessionHandle> {
    const config = resolveConfig(options as ResolveConfigOptions);
    const factory =
      options.mcpClientFactory ??
      ((c: Config) => Promise.reject<never>(noDefaultFactoryError(c)));
    return HubSession.open(config, factory);
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
