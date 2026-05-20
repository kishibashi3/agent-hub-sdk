// Mirror of Python's ``tests/test_messages.py``. Lenient JSON parser
// coverage: well-formed batches, schema drift, top-level shape errors.

import { describe, expect, it } from "vitest";

import { parseMessages, parseParticipants } from "../src/index.js";

describe("parseMessages", () => {
  it("parses well-formed batch", () => {
    const payload = JSON.stringify([
      {
        id: "msg-1",
        from: "@alice",
        to: "@me",
        message: "hello",
        timestamp: "2026-05-19T10:00:00Z",
      },
      {
        id: "msg-2",
        from: "@bob",
        to: "@me",
        message: "second",
        timestamp: "2026-05-19T10:00:01Z",
      },
    ]);
    const result = parseMessages(payload);
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({
      id: "msg-1",
      sender: "@alice",
      to: "@me",
      body: "hello",
      timestamp: "2026-05-19T10:00:00Z",
    });
  });

  it("skips malformed entries", () => {
    const payload = JSON.stringify([
      {
        id: "ok",
        from: "@a",
        to: "@me",
        message: "yes",
        timestamp: "2026-05-19T10:00:00Z",
      },
      { id: "bad", from: "@a" }, // missing keys
      "not a dict",
    ]);
    const result = parseMessages(payload);
    expect(result).toHaveLength(1);
    expect(result[0]?.id).toBe("ok");
  });

  it("throws on top-level non-array", () => {
    expect(() => parseMessages(JSON.stringify({ id: "x" }))).toThrow(
      /Unexpected/,
    );
  });
});

describe("parseParticipants", () => {
  it("filters to persons", () => {
    const payload = JSON.stringify([
      {
        type: "person",
        name: "@alice",
        display_name: "Alice the planner",
        mode: "stateful",
        is_online: true,
      },
      {
        type: "team",
        name: "@discussion",
        members: ["@alice"],
      },
      {
        type: "person",
        name: "@bob",
        display_name: null,
        mode: null,
        is_online: false,
      },
    ]);
    const result = parseParticipants(payload);
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({
      name: "@alice",
      displayName: "Alice the planner",
      mode: "stateful",
      isOnline: true,
    });
    expect(result[1]).toEqual({
      name: "@bob",
      displayName: null,
      mode: null,
      isOnline: false,
    });
  });

  it("handles partial drift", () => {
    const payload = JSON.stringify([
      { type: "person", name: "@a" }, // minimal but valid
      { type: "person" }, // missing name → skip
      { type: "person", name: "" }, // empty name → skip
      { type: "person", name: "@b", display_name: 42 }, // bad type → null
      "stray scalar", // not a dict → skip
    ]);
    const result = parseParticipants(payload);
    expect(result.map((p) => p.name)).toEqual(["@a", "@b"]);
    expect(result[0]?.isOnline).toBe(false);
    expect(result[1]?.displayName).toBeNull();
  });

  it("throws on top-level non-array", () => {
    expect(() => parseParticipants(JSON.stringify({ name: "@a" }))).toThrow(
      /Unexpected/,
    );
  });
});
