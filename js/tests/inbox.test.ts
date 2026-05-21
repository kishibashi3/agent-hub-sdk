// Coverage for ``HubSession.inbox()`` (M2) and the drain helpers.
//
// The iterator merges three concurrent producers (SSE push, safety-net
// poll, heartbeat) into a single output stream and optionally intercepts
// ``/ping`` messages at the SDK layer. Tests use stub MCP clients and
// a controlled notification handler to verify each path independently.
//
// Mirrors Python's ``tests/test_inbox_iterator.py`` 1:1.

import { describe, expect, it } from "vitest";

import {
  HubSession,
  type Config,
  type IncomingMessage,
  type McpClient,
  type McpNotification,
} from "../src/index.js";

// ------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------

function mkConfig(): Config {
  return {
    user: "me",
    displayName: null,
    mode: "stateful",
    tenant: null,
    url: "https://hub.example/mcp",
    pat: "ghp_xxx",
  };
}

type RawMsg = {
  id: string;
  from: string;
  to: string;
  message: string;
  timestamp: string;
};

function mkRawMsg(id: string, body: string, from = "@alice"): RawMsg {
  return {
    id,
    from,
    to: "@me",
    message: body,
    timestamp: "2026-05-20T10:00:00Z",
  };
}

/** Build a stub HubSession wired up for inbox iterator tests. */
function makeInboxSession(opts: {
  /** Successive ``get_messages`` batches (popped on each call). */
  unreadBatches?: RawMsg[][];
  /**
   * When set, every ``get_messages`` call returns this list regardless
   * of how many times it has been called (simulates "still unread"
   * because the consumer hasn't acked yet — used in dedup tests).
   */
  alwaysReturn?: RawMsg[];
  /** Error thrown by ``listTools`` (= heartbeat failure). */
  listToolsThrows?: Error;
} = {}): {
  session: HubSession;
  calls: Array<{ tool: string; args: Record<string, unknown> }>;
  listToolsCalls: { value: number };
  notify: (uri: string) => void;
} {
  const batches = [...(opts.unreadBatches ?? [])];
  const calls: Array<{ tool: string; args: Record<string, unknown> }> = [];
  const listToolsCalls = { value: 0 };
  let notifyHandler: ((n: McpNotification) => void) | undefined;
  const listToolsThrows = opts.listToolsThrows;

  const client: McpClient = {
    async initialize() {},
    async close() {},
    async callTool(name, args) {
      calls.push({ tool: name, args });
      if (name === "get_messages") {
        const batch =
          opts.alwaysReturn !== undefined
            ? opts.alwaysReturn
            : (batches.shift() ?? []);
        return {
          content: [{ type: "text" as const, text: JSON.stringify(batch) }],
          isError: false,
        };
      }
      if (name === "send_message") {
        return { content: [{ type: "text" as const, text: "ok" }], isError: false };
      }
      // mark_as_read, etc.
      return { content: [{ type: "text" as const, text: "" }], isError: false };
    },
    async subscribeResource() {},
    async listTools() {
      listToolsCalls.value++;
      if (listToolsThrows !== undefined) throw listToolsThrows;
      return {};
    },
    setNotificationHandler(h) {
      notifyHandler = h;
    },
  };

  const session = new HubSession(client, mkConfig());

  const notify = (uri: string): void => {
    notifyHandler?.({
      method: "notifications/resources/updated",
      params: { uri },
    });
  };

  return { session, calls, listToolsCalls, notify };
}

// Collect up to ``count`` messages from the iterator, enforcing a
// wall-clock timeout so stuck tests fail fast.
async function collectN(
  gen: AsyncGenerator<IncomingMessage, void, void>,
  count: number,
  timeoutMs = 3000,
): Promise<IncomingMessage[]> {
  const msgs: IncomingMessage[] = [];
  const timedOut = { flag: false };
  const timer = setTimeout(() => (timedOut.flag = true), timeoutMs);
  try {
    for await (const msg of gen) {
      msgs.push(msg);
      if (msgs.length >= count) break;
      if (timedOut.flag) throw new Error(`collectN: timeout after ${timeoutMs}ms`);
    }
  } finally {
    clearTimeout(timer);
  }
  return msgs;
}

// ------------------------------------------------------------------
// _drainInterceptingPing (private, accessed via cast)
// ------------------------------------------------------------------

describe("HubSession._drainInterceptingPing", () => {
  it("autoPong=false passes all messages through unchanged", async () => {
    const { session } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "/ping"), mkRawMsg("m2", "hello")]],
    });
    const drain = (session as unknown as {
      _drainInterceptingPing(autoPong: boolean): Promise<IncomingMessage[]>;
    })._drainInterceptingPing.bind(session);

    const result = await drain(false);
    expect(result.map((m) => m.id)).toEqual(["m1", "m2"]);
  });

  it("intercepts /ping: sends pong to sender, acks, drops from results", async () => {
    const { session, calls } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "/ping", "@operator")]],
    });
    const drain = (session as unknown as {
      _drainInterceptingPing(autoPong: boolean): Promise<IncomingMessage[]>;
    })._drainInterceptingPing.bind(session);

    const result = await drain(true);

    // /ping filtered out of results.
    expect(result).toHaveLength(0);

    // /pong sent to the original sender.
    const sendCall = calls.find((c) => c.tool === "send_message");
    expect(sendCall).toBeDefined();
    expect(sendCall?.args).toEqual({ to: "@operator", message: "pong" });

    // Ack issued for the /ping message.
    const ackCall = calls.find((c) => c.tool === "mark_as_read");
    expect(ackCall).toBeDefined();
    expect(ackCall?.args).toEqual({ message_id: "m1" });
  });

  it("whitespace around /ping still triggers interception", async () => {
    const { session } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "  /ping\n")]],
    });
    const drain = (session as unknown as {
      _drainInterceptingPing(autoPong: boolean): Promise<IncomingMessage[]>;
    })._drainInterceptingPing.bind(session);

    const result = await drain(true);
    expect(result).toHaveLength(0);
  });

  it("/ping with suffix (e.g. '/ping foo') is NOT intercepted", async () => {
    const { session } = makeInboxSession({
      unreadBatches: [
        [mkRawMsg("m1", "hello"), mkRawMsg("m2", "/ping suffix")],
      ],
    });
    const drain = (session as unknown as {
      _drainInterceptingPing(autoPong: boolean): Promise<IncomingMessage[]>;
    })._drainInterceptingPing.bind(session);

    const result = await drain(true);
    // "/ping suffix" is not exact-match /ping, stays in results.
    expect(result.map((m) => m.id)).toEqual(["m1", "m2"]);
  });

  it("pong send failure is swallowed — /ping still dropped from results", async () => {
    // A custom client that throws on send_message but succeeds on others.
    let notifyHandler: ((n: McpNotification) => void) | undefined;
    let getMessagesCallCount = 0;
    const client: McpClient = {
      async initialize() {},
      async close() {},
      async callTool(name) {
        if (name === "get_messages") {
          getMessagesCallCount++;
          return {
            content: [
              {
                type: "text" as const,
                text: JSON.stringify([mkRawMsg("m1", "/ping")]),
              },
            ],
            isError: false,
          };
        }
        if (name === "send_message") {
          throw new ConnectionError("temporary send failure");
        }
        return { content: [{ type: "text" as const, text: "" }], isError: false };
      },
      async subscribeResource() {},
      async listTools() { return {}; },
      setNotificationHandler(h) {
        notifyHandler = h;
      },
    };
    void notifyHandler;
    const session = new HubSession(client, mkConfig());
    const drain = (session as unknown as {
      _drainInterceptingPing(autoPong: boolean): Promise<IncomingMessage[]>;
    })._drainInterceptingPing.bind(session);

    // Should not throw; /ping is still filtered even though send failed.
    const result = await drain(true);
    expect(result).toHaveLength(0);
    expect(getMessagesCallCount).toBe(1);
  });
});

// ------------------------------------------------------------------
// inbox() — startup + producers
// ------------------------------------------------------------------

describe("HubSession.inbox — startup and producers", () => {
  it("yields messages from the startup drain (pre-subscribe unread)", async () => {
    const { session } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "hello")]],
    });

    const msgs = await collectN(
      session.inbox({ pollIntervalMs: 999_999, heartbeatIntervalMs: 999_999 }),
      1,
    );
    expect(msgs.map((m) => m.id)).toEqual(["m1"]);
  }, 5_000);

  it("yields messages delivered via SSE push after startup", async () => {
    // Startup drain returns empty; a push triggers the second call.
    const { session, notify } = makeInboxSession({
      unreadBatches: [[], [mkRawMsg("m1", "from push")]],
    });

    const received: string[] = [];

    const consume = async (): Promise<void> => {
      for await (const msg of session.inbox({
        pollIntervalMs: 999_999,
        heartbeatIntervalMs: 999_999,
      })) {
        received.push(msg.id);
        break;
      }
    };

    const pushLater = async (): Promise<void> => {
      await new Promise((r) => setTimeout(r, 50));
      notify("inbox://@me");
    };

    await Promise.all([consume(), pushLater()]);
    expect(received).toEqual(["m1"]);
  }, 5_000);

  it("safety-net poll picks up messages when SSE push is silent", async () => {
    // Startup: empty; poll at 50ms picks up m1.
    const { session } = makeInboxSession({
      unreadBatches: [[], [mkRawMsg("m1", "from poll")]],
    });

    const msgs = await collectN(
      session.inbox({ pollIntervalMs: 50, heartbeatIntervalMs: 999_999 }),
      1,
    );
    expect(msgs.map((m) => m.id)).toEqual(["m1"]);
  }, 5_000);

  it("autoPong default (true) intercepts /ping — not yielded to consumer", async () => {
    const { session, calls } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "/ping"), mkRawMsg("m2", "hello")]],
    });

    const msgs = await collectN(
      session.inbox({ pollIntervalMs: 999_999, heartbeatIntervalMs: 999_999 }),
      1,
    );

    // Only the non-/ping message reaches the consumer.
    expect(msgs.map((m) => m.id)).toEqual(["m2"]);

    // /pong was sent (autoPong=true).
    const sendCalls = calls.filter((c) => c.tool === "send_message");
    expect(
      sendCalls.some(
        (c) => (c.args as Record<string, unknown>).message === "pong",
      ),
    ).toBe(true);
  }, 5_000);

  it("autoPong=false yields /ping to the consumer unchanged", async () => {
    const { session, calls } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "/ping")]],
    });

    const msgs = await collectN(
      session.inbox({
        autoPong: false,
        pollIntervalMs: 999_999,
        heartbeatIntervalMs: 999_999,
      }),
      1,
    );

    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.id).toBe("m1");
    expect(msgs[0]?.body).toBe("/ping");

    // No /pong sent — the SDK got out of the way.
    const sendCalls = calls.filter((c) => c.tool === "send_message");
    expect(sendCalls).toHaveLength(0);
  }, 5_000);

  it("heartbeat (listTools) fires periodically within the inbox lifetime", async () => {
    // Startup delivers m1; consumer sleeps 150ms to let heartbeat fire.
    const { session, listToolsCalls } = makeInboxSession({
      unreadBatches: [[mkRawMsg("m1", "hello")]],
    });

    for await (const msg of session.inbox({
      autoPong: false,
      pollIntervalMs: 999_999,
      heartbeatIntervalMs: 50,
    })) {
      void msg;
      // Wait long enough for at least one heartbeat (50ms interval).
      await new Promise((r) => setTimeout(r, 150));
      break;
    }

    expect(listToolsCalls.value).toBeGreaterThanOrEqual(1);
  }, 5_000);

  it("heartbeat failure propagates out of the inbox iterator", async () => {
    // Startup drain empty; heartbeat throws after 10ms.
    const { session } = makeInboxSession({
      unreadBatches: [[]],
      listToolsThrows: new Error("session lost"),
    });

    await expect(async () => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      for await (const _ of session.inbox({
        autoPong: false,
        pollIntervalMs: 999_999,
        heartbeatIntervalMs: 10,
      })) {
        // No messages — startup empty; should never yield.
      }
    }).rejects.toThrow("session lost");
  }, 5_000);
});

// ------------------------------------------------------------------
// In-flight dedup (issue #31)
// ------------------------------------------------------------------

describe("HubSession.inbox — in-flight dedup (issue #31)", () => {
  it("SSE replay does not double-yield the same message ID", async () => {
    // Every ``get_messages`` call returns m1 (simulates "still unread"
    // because the consumer hasn't acked yet). Two pushes fire while m1
    // is being processed, but m1 should appear exactly once in output.
    const m1 = mkRawMsg("m1", "hello");
    let drainCallCount = 0;
    let notifyHandler: ((n: McpNotification) => void) | undefined;

    const client: McpClient = {
      async initialize() {},
      async close() {},
      async callTool(name) {
        if (name === "get_messages") {
          drainCallCount++;
          return {
            content: [{ type: "text" as const, text: JSON.stringify([m1]) }],
            isError: false,
          };
        }
        return { content: [{ type: "text" as const, text: "" }], isError: false };
      },
      async subscribeResource() {},
      async listTools() { return {}; },
      setNotificationHandler(h) {
        notifyHandler = h;
      },
    };

    const notify = (uri: string): void => {
      notifyHandler?.({
        method: "notifications/resources/updated",
        params: { uri },
      });
    };

    const session = new HubSession(client, mkConfig());
    const received: string[] = [];

    const consume = async (): Promise<void> => {
      for await (const msg of session.inbox({
        autoPong: false,
        pollIntervalMs: 999_999,
        heartbeatIntervalMs: 999_999,
      })) {
        received.push(msg.id);
        // Simulate slow processing (ack not yet called).
        await new Promise((r) => setTimeout(r, 50));
        // Fire second push while processing — m1 still unread.
        notify("inbox://@me");
        await new Promise((r) => setTimeout(r, 50));
        break;
      }
    };

    // Fire the first push shortly after inbox starts (after startup drain).
    const fireFirstPush = async (): Promise<void> => {
      await new Promise((r) => setTimeout(r, 20));
      notify("inbox://@me");
    };

    await Promise.all([consume(), fireFirstPush()]);

    // At least 2 drain calls confirms dedup was exercised (startup + ≥1 push).
    expect(drainCallCount).toBeGreaterThanOrEqual(2);
    // m1 appears exactly once despite multiple drains returning it.
    expect(received).toEqual(["m1"]);
  }, 5_000);

  it("two distinct message IDs from successive drains both yield", async () => {
    // Startup empty; two pushes each trigger a drain returning a different ID.
    const { session, notify } = makeInboxSession({
      unreadBatches: [
        [],
        [mkRawMsg("m1", "first")],
        [mkRawMsg("m2", "second")],
      ],
    });

    const received: string[] = [];

    const consume = async (): Promise<void> => {
      for await (const msg of session.inbox({
        autoPong: false,
        pollIntervalMs: 999_999,
        heartbeatIntervalMs: 999_999,
      })) {
        received.push(msg.id);
        if (received.length >= 2) break;
      }
    };

    const firePushes = async (): Promise<void> => {
      await new Promise((r) => setTimeout(r, 20));
      notify("inbox://@me"); // drain → m1
      await new Promise((r) => setTimeout(r, 20));
      notify("inbox://@me"); // drain → m2
    };

    await Promise.all([consume(), firePushes()]);
    expect(received).toEqual(["m1", "m2"]);
  }, 5_000);
});

// ------------------------------------------------------------------
// Poll interval env override
// ------------------------------------------------------------------

describe("HubSession.inbox — poll interval env override", () => {
  it("AGENT_HUB_INBOX_POLL_INTERVAL_S env var drives the poll interval", async () => {
    // Tight env override: 50ms poll triggers without a kwarg override.
    const envKey = "AGENT_HUB_INBOX_POLL_INTERVAL_S";
    const origEnv = process.env[envKey];
    process.env[envKey] = "0.05"; // 50ms

    try {
      const { session } = makeInboxSession({
        unreadBatches: [[], [mkRawMsg("m1", "from env-tuned poll")]],
      });

      const msgs = await collectN(
        session.inbox({
          autoPong: false,
          heartbeatIntervalMs: 999_999,
          // pollIntervalMs intentionally omitted → driven by env
        }),
        1,
      );
      expect(msgs.map((m) => m.id)).toEqual(["m1"]);
    } finally {
      if (origEnv === undefined) {
        delete process.env[envKey];
      } else {
        process.env[envKey] = origEnv;
      }
    }
  }, 5_000);

  it("AGENT_HUB_INBOX_POLL_INTERVAL_S with an invalid value throws (fail-fast, mirrors Python ValueError)", async () => {
    // Python raises ``ValueError`` on an unparseable env value; the TS
    // port must fail-fast rather than silently fall back to 30 s.
    const envKey = "AGENT_HUB_INBOX_POLL_INTERVAL_S";
    const origEnv = process.env[envKey];
    process.env[envKey] = "abc"; // not a number

    try {
      const { session } = makeInboxSession({ unreadBatches: [[]] });
      await expect(async () => {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        for await (const _ of session.inbox({ heartbeatIntervalMs: 999_999 })) {
          break; // never reached — the throw happens before the first yield
        }
      }).rejects.toThrow(/AGENT_HUB_INBOX_POLL_INTERVAL_S/);
    } finally {
      if (origEnv === undefined) {
        delete process.env[envKey];
      } else {
        process.env[envKey] = origEnv;
      }
    }
  }, 5_000);
});

// ------------------------------------------------------------------
// Internal ConnectionError stub (for pong-send-failure test)
// ------------------------------------------------------------------

class ConnectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConnectionError";
  }
}
