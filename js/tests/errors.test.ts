// Mirror of Python's ``tests/test_errors.py``. Covers the EN + JA
// pattern matrix in ``classifyHubError`` plus error-class identity
// assertions.

import { describe, expect, it } from "vitest";

import {
  ConfigurationError,
  HubTransientError,
  PeerNotFoundError,
  classifyHubError,
} from "../src/index.js";

describe("classifyHubError", () => {
  describe("peer_not_found patterns", () => {
    it.each([
      "peer @gemma not found",
      "no such peer",
      "@bot is offline",
      "Not Registered", // case-insensitive
      "宛先 @list は存在しません", // observed in production
      "peer が見つかりません",
      "@gemma は登録されていません",
      "オフライン状態です",
    ])("classifies %s as peer_not_found", (text) => {
      expect(classifyHubError(text)).toBe("peer_not_found");
    });
  });

  describe("transient patterns", () => {
    it.each([
      "HTTP 502 Bad Gateway",
      "503 service unavailable",
      "504 Gateway Timeout",
      "request timed out",
      "connection refused",
      "network unreachable",
      "please try again later",
      "agent-hub が応答していません",
      "タイムアウトしました",
      "一時的なエラー",
      "しばらくしてから再試行してください",
    ])("classifies %s as transient", (text) => {
      expect(classifyHubError(text)).toBe("transient");
    });
  });

  describe("unknown / empty", () => {
    it.each([
      "schema validation failed",
      "permission denied",
      "internal logic error: foo > bar",
      "",
      null,
      undefined,
    ])("classifies %p as unknown", (text) => {
      expect(classifyHubError(text)).toBe("unknown");
    });
  });
});

describe("error classes", () => {
  it("PeerNotFoundError carries peer + detail", () => {
    const err = new PeerNotFoundError("@gemma", "offline");
    expect(err.peer).toBe("@gemma");
    expect(err.detail).toBe("offline");
    expect(err.message).toContain("@gemma");
    expect(err.message).toContain("offline");
    expect(err).toBeInstanceOf(PeerNotFoundError);
    expect(err).toBeInstanceOf(Error);
  });

  it("HubTransientError is an Error subclass", () => {
    const err = new HubTransientError("503");
    expect(err).toBeInstanceOf(HubTransientError);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("HubTransientError");
  });

  it("ConfigurationError is an Error subclass", () => {
    const err = new ConfigurationError("missing");
    expect(err).toBeInstanceOf(ConfigurationError);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("ConfigurationError");
  });
});
