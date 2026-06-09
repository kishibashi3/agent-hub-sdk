// ``HubSession`` smoke tests via a stub MCP client. Mirrors the
// strategy of Python's ``test_session.py`` (= mock the MCP layer,
// exercise the SDK methods).

import { describe, expect, it, vi } from "vitest";

import {
  AgentHub,
  ConfigurationError,
  HubSession,
  HubTransientError,
  ParticipantNotFoundError,
  type Config,
  type McpClient,
  type McpNotification,
} from "../src/index.js";

function mkConfig(overrides: Partial<Config> = {}): Config {
  return {
    participant: "me",
    displayName: null,
    mode: "stateful",
    tenant: null,
    url: "https://hub.example/mcp",
    pat: "ghp_xxx",
    ...overrides,
  };
}

interface StubBehaviour {
  toolResponses?: Record<
    string,
    { content: Array<{ type: "text"; text: string }>; isError?: boolean }
  >;
  callToolThrows?: Record<string, Error>;
  listToolsThrows?: Error;
}

function stubClient(behaviour: StubBehaviour = {}): {
  client: McpClient;
  calls: Array<{ tool: string; args: Record<string, unknown> }>;
  notify: (n: McpNotification) => void;
} {
  const calls: Array<{ tool: string; args: Record<string, unknown> }> = [];
  let notifyHandler: ((n: McpNotification) => void) | undefined;
  const client: McpClient = {
    async initialize() {},
    async close() {},
    async callTool(name, args) {
      calls.push({ tool: name, args });
      const thrown = behaviour.callToolThrows?.[name];
      if (thrown !== undefined) throw thrown;
      const response = behaviour.toolResponses?.[name];
      if (response !== undefined) return response;
      return { content: [{ type: "text", text: "" }], isError: false };
    },
    async subscribeResource() {},
    async listTools() {
      if (behaviour.listToolsThrows !== undefined) {
        throw behaviour.listToolsThrows;
      }
      return {};
    },
    setNotificationHandler(handler) {
      notifyHandler = handler;
    },
  };
  return {
    client,
    calls,
    notify(n) {
      notifyHandler?.(n);
    },
  };
}

describe("HubSession.register", () => {
  it("calls register tool with displayName fallback", async () => {
    const { client, calls } = stubClient({
      toolResponses: {
        register: { content: [{ type: "text", text: "registered: @me" }] },
      },
    });
    const session = new HubSession(client, mkConfig({ displayName: "My Role" }));
    const text = await session.register();
    expect(text).toBe("registered: @me");
    expect(calls).toEqual([
      {
        tool: "register",
        args: { name: "me", display_name: "My Role" },
      },
    ]);
  });

  it("falls back to participant when displayName is null", async () => {
    const { client, calls } = stubClient({
      toolResponses: {
        register: { content: [{ type: "text", text: "ok" }] },
      },
    });
    const session = new HubSession(client, mkConfig());
    await session.register();
    expect(calls[0]?.args.display_name).toBe("me");
  });
});

describe("HubSession.send", () => {
  it("success returns text", async () => {
    const { client } = stubClient({
      toolResponses: {
        send_message: { content: [{ type: "text", text: "delivered" }] },
      },
    });
    const session = new HubSession(client, mkConfig());
    expect(await session.send("@peer", "hi")).toBe("delivered");
  });

  it("classifies peer-not-found", async () => {
    const { client } = stubClient({
      toolResponses: {
        send_message: {
          content: [{ type: "text", text: "宛先 @gemma は存在しません" }],
          isError: true,
        },
      },
    });
    const session = new HubSession(client, mkConfig());
    await expect(session.send("@gemma", "hi")).rejects.toBeInstanceOf(
      ParticipantNotFoundError,
    );
  });

  it("classifies transient", async () => {
    const { client } = stubClient({
      toolResponses: {
        send_message: {
          content: [{ type: "text", text: "503 service unavailable" }],
          isError: true,
        },
      },
    });
    const session = new HubSession(client, mkConfig());
    await expect(session.send("@peer", "hi")).rejects.toBeInstanceOf(
      HubTransientError,
    );
  });

  it("transport error becomes transient", async () => {
    const { client } = stubClient({
      callToolThrows: { send_message: new Error("connection refused") },
    });
    const session = new HubSession(client, mkConfig());
    await expect(session.send("@peer", "hi")).rejects.toBeInstanceOf(
      HubTransientError,
    );
  });
});

describe("HubSession.sendWithRetry", () => {
  it("succeeds on first try", async () => {
    const { client, calls } = stubClient({
      toolResponses: {
        send_message: { content: [{ type: "text", text: "ok" }] },
      },
    });
    const session = new HubSession(client, mkConfig());
    expect(await session.sendWithRetry("@peer", "hi")).toBe("ok");
    expect(calls).toHaveLength(1);
  });

  it("retries on transient then succeeds", async () => {
    // 2 transients → 1 success.
    const responses = [
      { content: [{ type: "text" as const, text: "503" }], isError: true },
      { content: [{ type: "text" as const, text: "timed out" }], isError: true },
      { content: [{ type: "text" as const, text: "delivered" }], isError: false },
    ];
    let idx = 0;
    const client: McpClient = {
      async initialize() {},
      async close() {},
      async callTool() {
        return responses[idx++]!;
      },
      async subscribeResource() {},
      async listTools() {
        return {};
      },
      setNotificationHandler() {},
    };
    const session = new HubSession(client, mkConfig());
    const slept: number[] = [];
    const text = await session.sendWithRetry("@peer", "hi", {
      baseDelayMs: 1,
      sleepFn: async (ms) => {
        slept.push(ms);
      },
    });
    expect(text).toBe("delivered");
    expect(slept).toEqual([1, 2]);
  });

  it("gives up after maxAttempts", async () => {
    const { client, calls } = stubClient({
      toolResponses: {
        send_message: {
          content: [{ type: "text", text: "503" }],
          isError: true,
        },
      },
    });
    const session = new HubSession(client, mkConfig());
    await expect(
      session.sendWithRetry("@peer", "hi", {
        maxAttempts: 3,
        baseDelayMs: 1,
        sleepFn: async () => {},
      }),
    ).rejects.toBeInstanceOf(HubTransientError);
    expect(calls).toHaveLength(3);
  });

  it("peer-not-found skips retry", async () => {
    const { client, calls } = stubClient({
      toolResponses: {
        send_message: {
          content: [{ type: "text", text: "not registered" }],
          isError: true,
        },
      },
    });
    const session = new HubSession(client, mkConfig());
    const slept: number[] = [];
    await expect(
      session.sendWithRetry("@peer", "hi", {
        sleepFn: async (ms) => {
          slept.push(ms);
        },
      }),
    ).rejects.toBeInstanceOf(ParticipantNotFoundError);
    expect(calls).toHaveLength(1);
    expect(slept).toEqual([]);
  });

  it("maxAttempts=1 means no retry", async () => {
    const { client, calls } = stubClient({
      toolResponses: {
        send_message: {
          content: [{ type: "text", text: "503 service unavailable" }],
          isError: true,
        },
      },
    });
    const session = new HubSession(client, mkConfig());
    const slept: number[] = [];
    await expect(
      session.sendWithRetry("@peer", "hi", {
        maxAttempts: 1,
        sleepFn: async (ms) => {
          slept.push(ms);
        },
      }),
    ).rejects.toBeInstanceOf(HubTransientError);
    expect(calls).toHaveLength(1);
    expect(slept).toEqual([]);
  });
});

describe("HubSession inbox operations", () => {
  it("getUnread parses payload", async () => {
    const payload = JSON.stringify([
      {
        id: "m1",
        from: "@a",
        to: "@me",
        message: "hi",
        timestamp: "2026-05-19T10:00:00Z",
      },
    ]);
    const { client } = stubClient({
      toolResponses: {
        get_messages: { content: [{ type: "text", text: payload }] },
      },
    });
    const session = new HubSession(client, mkConfig());
    const msgs = await session.getUnread();
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.id).toBe("m1");
    expect(msgs[0]?.sender).toBe("@a");
    expect(msgs[0]?.body).toBe("hi");
  });

  it("ack calls mark_as_read", async () => {
    const { client, calls } = stubClient({});
    const session = new HubSession(client, mkConfig());
    await session.ack("m1");
    expect(calls).toEqual([
      { tool: "mark_as_read", args: { message_id: "m1" } },
    ]);
  });

  it("subscribeInbox uses self URI", async () => {
    const subscribed: string[] = [];
    const client: McpClient = {
      async initialize() {},
      async close() {},
      async callTool() {
        return { content: [{ type: "text", text: "" }], isError: false };
      },
      async subscribeResource(uri) {
        subscribed.push(uri);
      },
      async listTools() {
        return {};
      },
      setNotificationHandler() {},
    };
    const session = new HubSession(client, mkConfig());
    await session.subscribeInbox();
    expect(subscribed).toEqual(["inbox://@me"]);
  });

  it("getParticipants filters teams", async () => {
    const payload = JSON.stringify([
      {
        type: "person",
        name: "@a",
        display_name: "A",
        mode: "stateful",
        is_online: true,
      },
      { type: "team", name: "@discussion", members: ["@a"] },
    ]);
    const { client } = stubClient({
      toolResponses: {
        get_participants: { content: [{ type: "text", text: payload }] },
      },
    });
    const session = new HubSession(client, mkConfig());
    const participants = await session.getParticipants();
    expect(participants).toHaveLength(1);
    expect(participants[0]?.name).toBe("@a");
  });

  it("heartbeat calls listTools", async () => {
    const { client } = stubClient({});
    const listToolsSpy = vi.spyOn(client, "listTools");
    const session = new HubSession(client, mkConfig());
    await session.heartbeat();
    expect(listToolsSpy).toHaveBeenCalledTimes(1);
  });
});

describe("HubSession.setStatus + status getter", () => {
  it("default status is idle", () => {
    const { client } = stubClient({});
    const session = new HubSession(client, mkConfig());
    expect(session.status).toBe("idle");
  });

  it("setStatus updates the value", () => {
    const { client } = stubClient({});
    const session = new HubSession(client, mkConfig());
    session.setStatus("busy");
    expect(session.status).toBe("busy");
  });

  it("setStatus rejects empty", () => {
    const { client } = stubClient({});
    const session = new HubSession(client, mkConfig());
    expect(() => session.setStatus("")).toThrow(/non-empty/);
  });
});

describe("HubSession.inboxPushes", () => {
  it("yields URIs delivered via the notification handler", async () => {
    const { client, notify } = stubClient({});
    const session = new HubSession(client, mkConfig());

    notify({
      method: "notifications/resources/updated",
      params: { uri: "inbox://@me" },
    });

    const iter = session.inboxPushes();
    const result = await iter.next();
    expect(result.value).toBe("inbox://@me");
  });

  it("ignores non-update notifications", async () => {
    const { client, notify } = stubClient({});
    const session = new HubSession(client, mkConfig());

    notify({ method: "notifications/initialized" });
    notify({
      method: "notifications/resources/updated",
      params: { uri: "inbox://@me" },
    });

    const iter = session.inboxPushes();
    const result = await iter.next();
    // Only the resource-update reaches the iterator
    expect(result.value).toBe("inbox://@me");
  });
});

describe("AgentHub.connect", () => {
  it("fails fast on missing config (no env override)", async () => {
    await expect(
      AgentHub.connect({ participant: "me", url: null, pat: null, env: {} }),
    ).rejects.toBeInstanceOf(ConfigurationError);
  });

  it("calls factory with resolved config when valid", async () => {
    const seen: Config[] = [];
    const handle = await AgentHub.connect({
      participant: "me",
      url: "https://hub.example/mcp",
      pat: "ghp",
      env: {},
      mcpClientFactory: async (cfg) => {
        seen.push(cfg);
        return stubClient({}).client;
      },
    });
    expect(seen).toHaveLength(1);
    expect(seen[0]?.url).toBe("https://hub.example/mcp");
    expect(seen[0]?.pat).toBe("ghp");
    await handle[Symbol.asyncDispose]();
  });

  // M5 (issue #27): `AgentHub.connect` auto-registers the consumer
  // before returning the handle. The three tests below pin the
  // behaviour planner GO'd in DM `ce8ef9ef` — call-on-connect,
  // raise-on-failure (fail-fast), post-handle passthrough.

  it("M5: auto-registers before returning the handle", async () => {
    const stub = stubClient({});
    const handle = await AgentHub.connect({
      participant: "alice",
      displayName: "Alice the Bridge",
      url: "https://hub.example/mcp",
      pat: "ghp",
      env: {},
      mcpClientFactory: async () => stub.client,
    });
    // Exactly one `register` call should have happened during connect.
    const registerCalls = stub.calls.filter((c) => c.tool === "register");
    expect(registerCalls).toHaveLength(1);
    expect(registerCalls[0]?.args).toEqual({
      name: "alice",
      display_name: "Alice the Bridge",
    });
    await handle[Symbol.asyncDispose]();
  });

  it("M5: falls back display_name to participant when not supplied", async () => {
    const stub = stubClient({});
    const handle = await AgentHub.connect({
      participant: "bob",
      // displayName intentionally omitted
      url: "https://hub.example/mcp",
      pat: "ghp",
      env: {},
      mcpClientFactory: async () => stub.client,
    });
    const registerCalls = stub.calls.filter((c) => c.tool === "register");
    expect(registerCalls).toHaveLength(1);
    expect(registerCalls[0]?.args.display_name).toBe("bob");
    await handle[Symbol.asyncDispose]();
  });

  it("M5: connect rejects when auto-register fails, no half-open handle", async () => {
    // Track that the factory's client gets `close()`d on the failure
    // path (= the just-opened MCP session is torn down before connect
    // rejects).
    let closeCount = 0;
    const stub = stubClient({
      callToolThrows: {
        register: new Error("server unavailable"),
      },
    });
    const wrapped: typeof stub.client = {
      ...stub.client,
      async close() {
        closeCount++;
        return stub.client.close();
      },
    };
    await expect(
      AgentHub.connect({
        participant: "carol",
        url: "https://hub.example/mcp",
        pat: "ghp",
        env: {},
        mcpClientFactory: async () => wrapped,
      }),
    ).rejects.toThrow(/transport failure|server unavailable/);
    // The failed-register path should have closed the underlying MCP
    // client (= handle[Symbol.asyncDispose]() ran before connect
    // rejected). Without this, a half-open MCP transport would leak.
    expect(closeCount).toBe(1);
  });

  it("M5: post-connect manual register passes through to server (idempotent)", async () => {
    // After connect, a manual `hub.session.register()` should still
    // work — the SDK does not gate duplicates; the server treats the
    // second call idempotently (refresh of display_name, harmless
    // re-confirm, etc.).
    const stub = stubClient({
      toolResponses: {
        register: { content: [{ type: "text", text: "registered: @dave" }] },
      },
    });
    const handle = await AgentHub.connect({
      participant: "dave",
      url: "https://hub.example/mcp",
      pat: "ghp",
      env: {},
      mcpClientFactory: async () => stub.client,
    });
    // Manual re-register after connect.
    const text = await handle.session.register();
    expect(text).toBe("registered: @dave");
    // Two register calls total: 1 auto + 1 manual.
    const registerCalls = stub.calls.filter((c) => c.tool === "register");
    expect(registerCalls).toHaveLength(2);
    await handle[Symbol.asyncDispose]();
  });
});
