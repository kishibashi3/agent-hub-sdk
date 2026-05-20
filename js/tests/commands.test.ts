// Mirror of Python's ``tests/test_commands.py``. Full 4-state ×
// yield/reject dispatch matrix + built-ins + override + help
// auto-generation.

import { describe, expect, it } from "vitest";

import {
  CommandRouter,
  parseCommand,
  type IncomingMessage,
} from "../src/index.js";

function mkMsg(body: string, id = "m1", sender = "@alice"): IncomingMessage {
  return {
    id,
    sender,
    to: "@me",
    body,
    timestamp: "2026-05-20T10:00:00Z",
  };
}

/** Recording fake matching the public surface ``CommandRouter`` uses. */
function mkHub() {
  const sends: Array<{ to: string; message: string }> = [];
  const acks: string[] = [];
  let status = "idle";
  return {
    sends,
    acks,
    // ``HubSession`` surface the router relies on:
    async send(to: string, message: string): Promise<string> {
      sends.push({ to, message });
      return "ok";
    },
    async ack(id: string): Promise<void> {
      acks.push(id);
    },
    get status() {
      return status;
    },
    setStatus(v: string) {
      status = v;
    },
  };
}

describe("parseCommand", () => {
  it("basic command", () => {
    expect(parseCommand("/list")).toEqual(["/list", ""]);
  });
  it("command with args", () => {
    expect(parseCommand("/list verbose")).toEqual(["/list", "verbose"]);
  });
  it("multi-token args", () => {
    expect(parseCommand("/add foo 0 * * *")).toEqual(["/add", "foo 0 * * *"]);
  });
  it("strips whitespace", () => {
    expect(parseCommand("  /ping  ")).toEqual(["/ping", ""]);
  });
  it("non-command body", () => {
    expect(parseCommand("hello world")).toEqual([null, ""]);
  });
  it("bare slash is not a command", () => {
    expect(parseCommand("/")).toEqual([null, ""]);
  });
  it.each([null, undefined, ""])("empty / nullish: %p", (v) => {
    expect(parseCommand(v)).toEqual([null, ""]);
  });
});

describe("CommandRouter registration", () => {
  it("rejects non-slash paths", () => {
    const router = new CommandRouter();
    expect(() => router.command("list", async () => null)).toThrow(/start with/);
  });
  it("rejects non-slash passthrough", () => {
    const router = new CommandRouter();
    expect(() => router.passthrough("help")).toThrow(/start with/);
  });
});

describe("dispatch — non-command", () => {
  it("yields plain text", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    expect(await router.dispatch(mkMsg("hello"), hub as never)).toBe("yield");
  });
});

describe("dispatch — user handler", () => {
  it("string return sends reply + acks", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.command("/active", async () => "working on M4");
    expect(await router.dispatch(mkMsg("/active"), hub as never)).toBe(
      "handled",
    );
    expect(hub.sends).toEqual([{ to: "@alice", message: "working on M4" }]);
    expect(hub.acks).toEqual(["m1"]);
  });

  it("null return acks but no auto-reply", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.command("/silent", async () => null);
    expect(await router.dispatch(mkMsg("/silent"), hub as never)).toBe(
      "handled",
    );
    expect(hub.sends).toEqual([]);
    expect(hub.acks).toEqual(["m1"]);
  });

  it("delivers args", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    const seen: string[] = [];
    router.command("/list", async (_msg, _hub, args) => {
      seen.push(args);
      return null;
    });
    await router.dispatch(mkMsg("/list verbose pending"), hub as never);
    expect(seen).toEqual(["verbose pending"]);
  });

  it("exception is swallowed + generic warning reply", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.command("/buggy", async () => {
      throw new Error("intentional");
    });
    expect(await router.dispatch(mkMsg("/buggy"), hub as never)).toBe(
      "handled",
    );
    expect(hub.sends).toHaveLength(1);
    expect(hub.sends[0]?.message).toContain("/buggy failed");
    expect(hub.acks).toEqual(["m1"]);
  });
});

describe("dispatch — built-in /ping", () => {
  it("replies plain pong", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    expect(await router.dispatch(mkMsg("/ping"), hub as never)).toBe("handled");
    // ⭐ M2.1 Rev 2: plain text "pong", not "/pong"
    expect(hub.sends).toEqual([{ to: "@alice", message: "pong" }]);
    expect(hub.acks).toEqual(["m1"]);
  });

  it("whitespace tolerant", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    await router.dispatch(mkMsg("  /ping\n"), hub as never);
    expect(hub.sends).toEqual([{ to: "@alice", message: "pong" }]);
  });

  it("user handler overrides built-in", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.command("/ping", async () => "/pong"); // legacy format
    await router.dispatch(mkMsg("/ping"), hub as never);
    expect(hub.sends).toEqual([{ to: "@alice", message: "/pong" }]);
  });

  it("builtins=false yields ping", async () => {
    const router = new CommandRouter({ builtins: false, unknown: "yield" });
    const hub = mkHub();
    expect(await router.dispatch(mkMsg("/ping"), hub as never)).toBe("yield");
    expect(hub.sends).toEqual([]);
  });
});

describe("dispatch — built-in /status", () => {
  it("returns default idle", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    await router.dispatch(mkMsg("/status"), hub as never);
    expect(hub.sends).toEqual([{ to: "@alice", message: "idle" }]);
  });

  it("reflects setStatus", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    hub.setStatus("busy");
    await router.dispatch(mkMsg("/status"), hub as never);
    expect(hub.sends).toEqual([{ to: "@alice", message: "busy" }]);
  });
});

describe("dispatch — built-in /help", () => {
  it("lists built-ins + user commands", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.command("/list", async () => null, { description: "list jobs" });
    router.command("/add", async () => null, { description: "add a job" });
    router.passthrough("/explain", { description: "ask the LLM" });

    await router.dispatch(mkMsg("/help"), hub as never);
    expect(hub.sends).toHaveLength(1);
    const body = hub.sends[0]!.message;
    expect(body).toContain("/ping");
    expect(body).toContain("/status");
    expect(body).toContain("/help");
    expect(body).toContain("/list");
    expect(body).toContain("list jobs");
    expect(body).toContain("/add");
    expect(body).toContain("add a job");
    expect(body).toContain("/explain");
    expect(body).toContain("ask the LLM");
  });

  it("user override replaces built-in", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.command("/help", async () => "custom help body");
    await router.dispatch(mkMsg("/help"), hub as never);
    expect(hub.sends).toEqual([{ to: "@alice", message: "custom help body" }]);
  });

  it("builtins=false + no handlers yields", async () => {
    const router = new CommandRouter({ builtins: false, unknown: "yield" });
    const hub = mkHub();
    expect(await router.dispatch(mkMsg("/help"), hub as never)).toBe("yield");
    expect(hub.sends).toEqual([]);
  });
});

describe("dispatch — passthrough", () => {
  it("yields to consumer", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.passthrough("/explain");
    expect(
      await router.dispatch(mkMsg("/explain me factorial"), hub as never),
    ).toBe("yield");
    expect(hub.sends).toEqual([]);
    expect(hub.acks).toEqual([]);
  });

  it("handler wins over passthrough", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    router.passthrough("/dual");
    router.command("/dual", async () => "handled-not-yielded");
    expect(await router.dispatch(mkMsg("/dual"), hub as never)).toBe("handled");
    expect(hub.sends[0]?.message).toBe("handled-not-yielded");
  });
});

describe("dispatch — unknown", () => {
  it("default reject sends plain command-not-found", async () => {
    const router = new CommandRouter();
    const hub = mkHub();
    expect(await router.dispatch(mkMsg("/foobar"), hub as never)).toBe(
      "handled",
    );
    expect(hub.sends).toEqual([
      { to: "@alice", message: "command not found: /foobar" },
    ]);
    expect(hub.acks).toEqual(["m1"]);
  });

  it("yield mode passes through", async () => {
    const router = new CommandRouter({ unknown: "yield" });
    const hub = mkHub();
    expect(await router.dispatch(mkMsg("/foobar"), hub as never)).toBe("yield");
    expect(hub.sends).toEqual([]);
    expect(hub.acks).toEqual([]);
  });

  it("rejectFormat customisable", async () => {
    const router = new CommandRouter({ rejectFormat: "unknown command: {cmd}" });
    const hub = mkHub();
    await router.dispatch(mkMsg("/foobar"), hub as never);
    expect(hub.sends).toEqual([
      { to: "@alice", message: "unknown command: /foobar" },
    ]);
  });
});
