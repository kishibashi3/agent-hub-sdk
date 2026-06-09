// Configuration resolution for ``AgentHub.connect``. Mirrors Python's
// ``agent_hub_sdk/config.py`` 1:1.
//
// Caller-supplied keyword args take precedence; environment variables
// are the fallback; **there is no implicit default URL**. If either is
// missing for a required field, ``ConfigurationError`` is thrown at
// connect time so the consumer surfaces the misconfiguration
// immediately instead of silently talking to ``localhost:3000`` or
// some other accidental endpoint.
//
// See ``docs/design.md`` §4 — this is redline #1, locked in at the
// design-doc level during the M0 cycle.

import { ConfigurationError } from "./errors.js";

/**
 * Resolved configuration for one ``AgentHub`` session. Built by
 * ``resolveConfig`` from caller args + environment. Treated as
 * immutable for the lifetime of the session.
 */
export interface Config {
  /** Handle without the leading ``@``. */
  readonly user: string;
  /** Human-readable role descriptor; null if unspecified. */
  readonly displayName: string | null;
  /** Tenant scope. ``null`` routes to the default tenant. */
  readonly tenant: string | null;
  /** agent-hub MCP endpoint. */
  readonly url: string;
  /** GitHub PAT used as the bearer token. */
  readonly pat: string;
}

// Environment variable names consulted when the caller does not pass
// a value. Kept exported so tests and bridges can reference the same
// strings without re-typing them.
export const ENV_URL = "AGENT_HUB_URL";
export const ENV_PAT = "GITHUB_PAT";
export const ENV_TENANT = "AGENT_HUB_TENANT";
export const ENV_DISPLAY_NAME = "AGENT_HUB_DISPLAY_NAME";

/** Options accepted by ``resolveConfig`` / ``AgentHub.connect``. */
export interface ResolveConfigOptions {
  user: string;
  tenant?: string | null;
  displayName?: string | null;
  url?: string | null;
  pat?: string | null;
  /** Environment-variable lookup table; defaults to ``process.env``. */
  env?: Record<string, string | undefined>;
}

/**
 * Merge caller args + environment into a ``Config``.
 *
 * Precedence: caller arg → environment → fail-fast. There is **no
 * implicit default URL** — missing ``url`` or ``pat`` throws
 * ``ConfigurationError`` immediately.
 */
export function resolveConfig(options: ResolveConfigOptions): Config {
  const env = options.env ?? (process.env as Record<string, string | undefined>);

  if (!options.user) {
    throw new ConfigurationError(
      "AgentHub.connect: 'user' is required and must be non-empty",
    );
  }

  const url = options.url ?? env[ENV_URL] ?? null;
  const pat = options.pat ?? env[ENV_PAT] ?? null;
  const tenant = options.tenant ?? env[ENV_TENANT] ?? null;
  const displayName = options.displayName ?? env[ENV_DISPLAY_NAME] ?? null;

  const missing: string[] = [];
  if (!url) {
    missing.push(`url (or env ${ENV_URL})`);
  }
  if (!pat) {
    missing.push(`pat (or env ${ENV_PAT})`);
  }
  if (missing.length > 0) {
    throw new ConfigurationError(
      "AgentHub.connect: missing required configuration: " +
        missing.join(", ") +
        ". Pass the value explicitly or set the environment variable. " +
        "There is no implicit default URL — the SDK will not fall back " +
        "to localhost or any other endpoint.",
    );
  }

  return {
    user: options.user,
    displayName,
    tenant,
    url: url!,
    pat: pat!,
  };
}

/**
 * Build the MCP HTTP headers for one ``Config``.
 *
 * Authorization is the GitHub PAT as a bearer token; ``X-User-Id``
 * declares the SDK consumer's handle; ``X-Tenant-Id`` is added only
 * when a tenant is set (the server treats absence as the default
 * tenant).
 */
export function makeHeaders(config: Config): Record<string, string> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${config.pat}`,
    "X-User-Id": config.user,
  };
  if (config.tenant) {
    headers["X-Tenant-Id"] = config.tenant;
  }
  return headers;
}
