"""Configuration resolution for ``AgentHub.connect``.

Caller-supplied keyword args take precedence; environment variables are the
fallback; **there is no implicit default URL**. If both are missing for a
required field, ``ConfigurationError`` is raised at connect time so the
consumer surfaces the misconfiguration immediately instead of silently
talking to ``localhost:3000`` or some other accidental endpoint.

See ``docs/design.md`` §4 — this is redline #1, locked in at the design-doc
level during the M0 cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from agent_hub_sdk.errors import ConfigurationError

__all__ = ["Config", "Mode", "resolve_config"]

#: Worker mode declared at register-time. ``stateful`` is the only mode
#: supported in M1; ``stateless`` lands in M3 and ``global`` in M5.
Mode = Literal["stateful", "stateless", "global"]


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one ``AgentHub`` session.

    Built by :func:`resolve_config` from caller args + environment. The SDK
    treats this as immutable for the lifetime of the session; the consumer
    can hold the same instance across reconnects.
    """

    #: Handle without the leading ``@`` (e.g. ``"my-bridge"``). The SDK
    #: prefixes ``@`` when calling tools that expect a handle.
    user: str

    #: Optional human-readable role descriptor; surfaced in
    #: ``get_participants``. Defaults to ``user`` if not supplied.
    display_name: str | None

    #: Worker-mode declaration. M1 only validates ``stateful``; other modes
    #: are accepted by the schema (so M3/M5 implementations can land
    #: incrementally) but currently behave the same as ``stateful``.
    mode: Mode

    #: Tenant scope. ``None`` routes to the default tenant.
    tenant: str | None

    #: agent-hub MCP endpoint (e.g. ``https://agent-hub-ki.fly.dev/mcp``).
    #: Required; missing → ``ConfigurationError``.
    url: str

    #: GitHub PAT used as the bearer token. Required; missing →
    #: ``ConfigurationError``.
    pat: str


# Environment variable names consulted when the caller does not pass a value.
ENV_URL = "AGENT_HUB_URL"
ENV_PAT = "GITHUB_PAT"
ENV_TENANT = "AGENT_HUB_TENANT"
ENV_DISPLAY_NAME = "AGENT_HUB_DISPLAY_NAME"


def resolve_config(
    *,
    user: str,
    mode: Mode = "stateful",
    tenant: str | None = None,
    display_name: str | None = None,
    url: str | None = None,
    pat: str | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Merge caller args + environment into a :class:`Config`.

    Precedence: caller arg → environment → fail-fast. There is **no implicit
    default URL** — missing ``url`` or ``pat`` raises
    :class:`~agent_hub_sdk.errors.ConfigurationError` immediately.

    :param user: SDK consumer's handle (without leading ``@``). Required.
    :param mode: worker mode declared at register-time.
    :param tenant: tenant scope, or ``None`` for the default tenant.
    :param display_name: human-readable role descriptor.
    :param url: agent-hub MCP endpoint. Falls back to ``AGENT_HUB_URL``.
    :param pat: GitHub PAT. Falls back to ``GITHUB_PAT``.
    :param env: environment-variable lookup table for testing. Defaults to
        :data:`os.environ`.
    :raises ConfigurationError: if ``user`` is empty, or if either ``url``
        or ``pat`` cannot be resolved from args + env.
    """
    env_map = os.environ if env is None else env

    if not user:
        raise ConfigurationError(
            "AgentHub.connect: 'user' is required and must be non-empty"
        )

    # ``or`` chains short-circuit to the first truthy value, or fall through
    # to the final operand. ``env_map.get(...)`` already returns ``None`` on
    # a missing key, so the trailing ``or None`` previously here was a
    # no-op and has been removed (PR #8 review, Minor 1).
    resolved_url = url or env_map.get(ENV_URL)
    resolved_pat = pat or env_map.get(ENV_PAT)
    resolved_tenant = tenant or env_map.get(ENV_TENANT)
    resolved_display = display_name or env_map.get(ENV_DISPLAY_NAME)

    missing: list[str] = []
    if not resolved_url:
        missing.append(f"url (or env {ENV_URL})")
    if not resolved_pat:
        missing.append(f"pat (or env {ENV_PAT})")
    if missing:
        raise ConfigurationError(
            "AgentHub.connect: missing required configuration: "
            + ", ".join(missing)
            + ". Pass the value explicitly or set the environment variable. "
            "There is no implicit default URL — the SDK will not fall back "
            "to localhost or any other endpoint."
        )

    return Config(
        user=user,
        display_name=resolved_display,
        mode=mode,
        tenant=resolved_tenant,
        url=resolved_url,  # type: ignore[arg-type]
        pat=resolved_pat,  # type: ignore[arg-type]
    )


def make_headers(config: Config) -> dict[str, str]:
    """Build the MCP HTTP headers for one :class:`Config`.

    Authorization is the GitHub PAT as a bearer token; ``X-User-Id`` declares
    the SDK consumer's handle; ``X-Tenant-Id`` is added only when a tenant is
    set (the server treats the absence as the default tenant).
    """
    headers = {
        "Authorization": f"Bearer {config.pat}",
        "X-User-Id": config.user,
    }
    if config.tenant:
        headers["X-Tenant-Id"] = config.tenant
    return headers
