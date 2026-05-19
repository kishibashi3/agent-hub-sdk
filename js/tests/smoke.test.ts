// Smoke test for the M0 skeleton. M4 will replace this with real coverage of
// `AgentHub.connect`, `inbox`, `send`, etc.

import { describe, expect, it } from "vitest";

import { VERSION } from "../src/index.js";

describe("agent-hub-sdk smoke", () => {
  it("exposes a SemVer-ish version", () => {
    expect(VERSION).toMatch(/^\d+\.\d+\.\d+/);
  });
});
