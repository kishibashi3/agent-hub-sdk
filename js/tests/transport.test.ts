// Mirror of Python's ``tests/test_transport.py``. Tool-result
// classification helpers: success/transient/peer-not-found/unknown.

import { describe, expect, it } from "vitest";

import {
  HubTransientError,
  ParticipantNotFoundError,
  extractText,
  raiseForSendError,
  raiseForToolError,
  type ToolResult,
} from "../src/index.js";

function textResult(text: string, isError = false): ToolResult {
  return {
    content: [{ type: "text", text }],
    isError,
  };
}

describe("extractText", () => {
  it("joins text blocks", () => {
    const result: ToolResult = {
      content: [
        { type: "text", text: "hello" },
        { type: "text", text: "world" },
      ],
      isError: false,
    };
    expect(extractText(result.content)).toBe("hello\nworld");
  });

  it("handles empty content", () => {
    expect(extractText(null)).toBe("");
    expect(extractText(undefined)).toBe("");
    expect(extractText([])).toBe("");
  });
});

describe("raiseForToolError", () => {
  it("returns text on success", () => {
    expect(raiseForToolError(textResult("ok body"), "register")).toBe("ok body");
  });

  it("throws HubTransientError on transient isError", () => {
    expect(() =>
      raiseForToolError(textResult("503 service unavailable", true), "register"),
    ).toThrow(HubTransientError);
  });

  it("throws generic Error on unknown isError", () => {
    let caught: unknown;
    try {
      raiseForToolError(textResult("schema mismatch foo", true), "register");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(Error);
    expect(caught).not.toBeInstanceOf(HubTransientError);
  });
});

describe("raiseForSendError", () => {
  it("returns text on success", () => {
    expect(raiseForSendError(textResult("delivered"), "@peer")).toBe(
      "delivered",
    );
  });

  it("throws ParticipantNotFoundError on peer-not-found", () => {
    let caught: unknown;
    try {
      raiseForSendError(textResult("宛先 @gemma は存在しません", true), "@gemma");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ParticipantNotFoundError);
    expect((caught as ParticipantNotFoundError).peer).toBe("@gemma");
    expect((caught as ParticipantNotFoundError).detail).toContain("存在しません");
  });

  it("throws HubTransientError on transient", () => {
    expect(() =>
      raiseForSendError(
        textResult("agent-hub が応答していません", true),
        "@gemma",
      ),
    ).toThrow(HubTransientError);
  });

  it("throws generic Error on unknown", () => {
    let caught: unknown;
    try {
      raiseForSendError(textResult("policy violation: blocked", true), "@gemma");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(Error);
    expect(caught).not.toBeInstanceOf(ParticipantNotFoundError);
    expect(caught).not.toBeInstanceOf(HubTransientError);
  });
});
