// Coverage for ``HubSession.oneShot()`` (M3).
//
// ``oneShot`` opens a fresh MCP session for one batch of tool calls,
// returning a ``HubSessionHandle`` that the caller disposes with
// ``await using`` or explicit ``Symbol.asyncDispose``.
//
// These tests don't hit the streamable-HTTP transport. They inject a
// ``McpClientFactory`` stub directly via ``session._factory`` and
// verify lifecycle, independence, and config passthrough.
//
// Mirrors Python's ``tests/test_one_shot.py`` 1:1.

import { describe, expect, it } from "vitest";

import {
  AgentHub,
  ConfigurationError,
  HubSession,
  type Config,
  type McpClient,
  type McpClientFactory,
} from "../src/index.js";

function mkConfig(overrides: Partial<Config> = {}): Config {
  return {
    participant: "me",
    displayName: null,
    mode: "stateless",
    tenant: null,
    url: "https://hub.example/mcp",
    pat: "ghp_xxx",
    ...overrides,
  };
}

function makeStubClient(opts: { onClose?: () => void } = {}): McpClient {
  return {
    async initialize() {},
    async close() {
      opts.onClose?.();
    },
    async callTool() {
      return { content: [{ type: "text" as const, text: "" }], isError: false };
    },
    async subscribeResource() {},
    async listTools() {
      return {};
    },
    setNotificationHandler() {},
  };
}

// Build a McpClientFactory that records every Config it's called with.
function makeRecordingFactory(opts: { onClose?: () => void } = {}): {
  factory: McpClientFactory;
  opens: Config[];
} {
  const opens: Config[] = [];
  const factory: McpClientFactory = async (cfg) => {
    opens.push(cfg);
    return makeStubClient({ onClose: opts.onClose });
  };
  return { factory, opens };
}

// ------------------------------------------------------------------
// No-factory error path
// ------------------------------------------------------------------

describe("HubSession.oneShot — no factory error", () => {
  it("throws when session was not opened via HubSession.open", async () => {
    const session = new HubSession(makeStubClient(), mkConfig());
    // _factory is undefined (= not opened via HubSession.open / AgentHub.connect)
    await expect(session.oneShot()).rejects.toThrow(/no factory/);
  });
});

// ------------------------------------------------------------------
// Lifecycle
// ------------------------------------------------------------------

describe("HubSession.oneShot — lifecycle", () => {
  it("returns a HubSessionHandle wrapping a distinct HubSession", async () => {
    const outer = new HubSession(makeStubClient(), mkConfig());
    const { factory, opens } = makeRecordingFactory();
    outer._factory = factory;

    const inner = await outer.oneShot();

    expect(inner.session).toBeInstanceOf(HubSession);
    expect(inner.session).not.toBe(outer);
    // Factory was called once with the outer's config.
    expect(opens).toHaveLength(1);
    expect(opens[0]).toEqual(outer.config);

    await inner[Symbol.asyncDispose]();
  });

  it("inner session carries the same Config (url, pat, tenant, user, mode)", async () => {
    const cfg = mkConfig({
      participant: "translator",
      tenant: "kaz",
      mode: "stateless",
      url: "https://hub.example/mcp",
      pat: "ghp_xyz",
    });
    const outer = new HubSession(makeStubClient(), cfg);
    const { factory, opens } = makeRecordingFactory();
    outer._factory = factory;

    const inner = await outer.oneShot();

    expect(opens).toHaveLength(1);
    expect(opens[0]?.participant).toBe("translator");
    expect(opens[0]?.tenant).toBe("kaz");
    expect(opens[0]?.mode).toBe("stateless");
    expect(opens[0]?.url).toBe("https://hub.example/mcp");
    expect(opens[0]?.pat).toBe("ghp_xyz");

    await inner[Symbol.asyncDispose]();
  });

  it("Symbol.asyncDispose closes the inner MCP client (normal exit)", async () => {
    let closed = false;
    const outer = new HubSession(makeStubClient(), mkConfig());
    outer._factory = async () => makeStubClient({ onClose: () => (closed = true) });

    const inner = await outer.oneShot();
    expect(closed).toBe(false);
    await inner[Symbol.asyncDispose]();
    expect(closed).toBe(true);
  });

  it("Symbol.asyncDispose runs even when the block throws (try/finally)", async () => {
    let closed = false;
    const outer = new HubSession(makeStubClient(), mkConfig());
    outer._factory = async () => makeStubClient({ onClose: () => (closed = true) });

    const run = async () => {
      const inner = await outer.oneShot();
      try {
        throw new Error("boom");
      } finally {
        await inner[Symbol.asyncDispose]();
      }
    };

    await expect(run()).rejects.toThrow("boom");
    expect(closed).toBe(true);
  });
});

// ------------------------------------------------------------------
// Independence from the outer session
// ------------------------------------------------------------------

describe("HubSession.oneShot — independence", () => {
  it("inner session status starts at 'idle' regardless of outer status", async () => {
    const outer = new HubSession(makeStubClient(), mkConfig());
    outer.setStatus("busy");

    outer._factory = async () => makeStubClient();

    const inner = await outer.oneShot();

    // Inner starts fresh at "idle".
    expect(inner.session.status).toBe("idle");
    // Outer's status is not altered.
    expect(outer.status).toBe("busy");

    // Changing inner's status does not affect outer.
    inner.session.setStatus("rate-limited");
    expect(outer.status).toBe("busy");

    await inner[Symbol.asyncDispose]();
    // Outer's status is still unchanged after inner disposes.
    expect(outer.status).toBe("busy");
  });

  it("inner session uses a different McpClient object from outer", async () => {
    const outerClient = makeStubClient();
    const outer = new HubSession(outerClient, mkConfig());
    outer._factory = async () => makeStubClient();

    const inner = await outer.oneShot();
    expect(inner.session.client).not.toBe(outerClient);
    await inner[Symbol.asyncDispose]();
  });

  it("push notification to outer does not reach inner push queue", async () => {
    // Wire a custom outer client so we can fire notifications directly.
    let outerNotify: ((n: { method?: string; params?: { uri?: string } }) => void) | undefined;
    const outerClient: McpClient = {
      ...makeStubClient(),
      setNotificationHandler(h) {
        outerNotify = h;
      },
    };
    const outer = new HubSession(outerClient, mkConfig());
    outer._factory = async () => makeStubClient();

    const inner = await outer.oneShot();

    // Fire a push to the outer session.
    outerNotify?.({
      method: "notifications/resources/updated",
      params: { uri: "inbox://@outer-only" },
    });

    // Outer's push queue should have 1 item; inner's should be empty.
    // Access private fields via cast to verify structural isolation.
    const outerAny = outer as unknown as { _pushQueue: string[] };
    const innerAny = inner.session as unknown as { _pushQueue: string[] };
    expect(outerAny._pushQueue).toHaveLength(1);
    expect(innerAny._pushQueue).toHaveLength(0);

    await inner[Symbol.asyncDispose]();
  });
});

// ------------------------------------------------------------------
// Nested inside AgentHub.connect
// ------------------------------------------------------------------

describe("HubSession.oneShot — nested inside AgentHub.connect", () => {
  it("two factory calls when oneShot is used inside connect", async () => {
    const opens: Config[] = [];

    const handle = await AgentHub.connect({
      participant: "translator",
      url: "https://hub.example/mcp",
      pat: "ghp_xxx",
      env: {},
      mcpClientFactory: async (cfg) => {
        opens.push(cfg);
        return makeStubClient();
      },
    });

    // First open: AgentHub.connect
    expect(opens).toHaveLength(1);

    // Nested oneShot: second open
    const innerHandle = await handle.session.oneShot();
    expect(opens).toHaveLength(2);

    // Both called with the same Config.
    expect(opens[0]).toEqual(opens[1]);
    expect(opens[0]?.participant).toBe("translator");

    expect(innerHandle.session).not.toBe(handle.session);

    await innerHandle[Symbol.asyncDispose]();
    await handle[Symbol.asyncDispose]();
  });

  it("AgentHub.connect still fails fast when url/pat are missing", async () => {
    // oneShot is built on top of HubSession.open, which gets its Config
    // from AgentHub.connect. ConfigurationError still fires pre-factory.
    await expect(
      AgentHub.connect({ participant: "me", url: null, pat: null, env: {} }),
    ).rejects.toBeInstanceOf(ConfigurationError);
  });
});
