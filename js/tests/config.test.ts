// Mirror of Python's ``tests/test_config.py``. Pins redline #1
// (fail-fast, no implicit default URL) and the kwarg/env precedence.

import { describe, expect, it } from "vitest";

import { ConfigurationError, makeHeaders, resolveConfig } from "../src/index.js";

describe("resolveConfig fail-fast", () => {
  it("throws when both url and pat are missing", () => {
    expect(() => resolveConfig({ user: "me", env: {} })).toThrow(
      ConfigurationError,
    );
    try {
      resolveConfig({ user: "me", env: {} });
    } catch (err) {
      const msg = (err as Error).message;
      expect(msg).toContain("url");
      expect(msg).toContain("pat");
      expect(msg).toContain("AGENT_HUB_URL");
      expect(msg).toContain("GITHUB_PAT");
      // Redline: no implicit fallback endpoint suggested
      expect(msg.toLowerCase()).not.toContain("http://localhost");
      expect(msg.toLowerCase()).not.toContain("defaulting to");
    }
  });

  it("throws when only url is missing", () => {
    expect(() =>
      resolveConfig({ user: "me", pat: "ghp_x", env: {} }),
    ).toThrow(/url/);
  });

  it("throws when only pat is missing", () => {
    expect(() =>
      resolveConfig({ user: "me", url: "https://hub/mcp", env: {} }),
    ).toThrow(/pat/);
  });

  it("throws when user is empty", () => {
    expect(() =>
      resolveConfig({ user: "", url: "https://hub/mcp", pat: "ghp", env: {} }),
    ).toThrow(/user/);
  });

  it("does not silently fall back to localhost", () => {
    // Even with everything else set, missing url is not auto-filled.
    expect(() =>
      resolveConfig({ user: "me", url: null, pat: "ghp", env: {} }),
    ).toThrow(ConfigurationError);
  });
});

describe("resolveConfig precedence", () => {
  it("kwarg wins over env", () => {
    const config = resolveConfig({
      user: "me",
      url: "https://kw-url/mcp",
      pat: "ghp_kw",
      env: { AGENT_HUB_URL: "https://env-url/mcp", GITHUB_PAT: "ghp_env" },
    });
    expect(config.url).toBe("https://kw-url/mcp");
    expect(config.pat).toBe("ghp_kw");
  });

  it("env used when kwarg absent", () => {
    const config = resolveConfig({
      user: "me",
      env: {
        AGENT_HUB_URL: "https://env-url/mcp",
        GITHUB_PAT: "ghp_env",
        AGENT_HUB_TENANT: "kaz",
        AGENT_HUB_DISPLAY_NAME: "Test Role",
      },
    });
    expect(config.url).toBe("https://env-url/mcp");
    expect(config.pat).toBe("ghp_env");
    expect(config.tenant).toBe("kaz");
    expect(config.displayName).toBe("Test Role");
  });

});

describe("makeHeaders", () => {
  it("emits required headers without tenant", () => {
    const headers = makeHeaders({
      user: "me",
      displayName: null,
      tenant: null,
      url: "https://hub/mcp",
      pat: "ghp_xxx",
    });
    expect(headers.Authorization).toBe("Bearer ghp_xxx");
    expect(headers["X-User-Id"]).toBe("me");
    expect(headers).not.toHaveProperty("X-Tenant-Id");
  });

  it("emits X-Tenant-Id when tenant is set", () => {
    const headers = makeHeaders({
      user: "me",
      displayName: null,
      tenant: "kaz",
      url: "https://hub/mcp",
      pat: "ghp_xxx",
    });
    expect(headers["X-Tenant-Id"]).toBe("kaz");
  });
});
