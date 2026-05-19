"""``resolve_config`` fail-fast behaviour — redline #1 instance #2.

These tests pin the design-doc-level commitment ("no implicit default URL")
into code-level enforcement. If a future change introduces a fallback to
``localhost:3000`` or similar, these tests fail loudly.
"""

from __future__ import annotations

import pytest

from agent_hub_sdk import ConfigurationError, resolve_config
from agent_hub_sdk.config import Config, make_headers


class TestResolveConfigFailFast:
    """Missing required config raises ``ConfigurationError`` immediately."""

    def test_both_url_and_pat_missing_raises(self) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            resolve_config(user="me", env={})
        msg = str(exc_info.value)
        assert "url" in msg
        assert "pat" in msg
        # The redline: no specific fallback endpoint gets suggested. The
        # message may *mention* localhost in the negative ("we will not
        # fall back to localhost"); we check for the suggestion form.
        assert "http://localhost" not in msg.lower()
        assert "defaulting to" not in msg.lower()
        # The message points the consumer at the env vars.
        assert "AGENT_HUB_URL" in msg
        assert "GITHUB_PAT" in msg

    def test_url_missing_only_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="url"):
            resolve_config(user="me", pat="ghp_xxx", env={})

    def test_pat_missing_only_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="pat"):
            resolve_config(user="me", url="https://hub/mcp", env={})

    def test_empty_user_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="user"):
            resolve_config(user="", url="https://hub/mcp", pat="ghp", env={})

    def test_no_implicit_localhost_fallback(self) -> None:
        # Even if everything else is set, an empty URL is not silently
        # filled in. This is the literal redline.
        with pytest.raises(ConfigurationError):
            resolve_config(user="me", url=None, pat="ghp", env={})


class TestResolveConfigPrecedence:
    """Caller args win over env; env wins over nothing."""

    def test_kwarg_wins_over_env(self) -> None:
        config = resolve_config(
            user="me",
            url="https://kw-url/mcp",
            pat="ghp_kw",
            env={"AGENT_HUB_URL": "https://env-url/mcp", "GITHUB_PAT": "ghp_env"},
        )
        assert config.url == "https://kw-url/mcp"
        assert config.pat == "ghp_kw"

    def test_env_used_when_kwarg_absent(self) -> None:
        config = resolve_config(
            user="me",
            env={
                "AGENT_HUB_URL": "https://env-url/mcp",
                "GITHUB_PAT": "ghp_env",
                "AGENT_HUB_TENANT": "kaz",
                "AGENT_HUB_DISPLAY_NAME": "Test Role",
            },
        )
        assert config.url == "https://env-url/mcp"
        assert config.pat == "ghp_env"
        assert config.tenant == "kaz"
        assert config.display_name == "Test Role"

    def test_default_mode_is_stateful(self) -> None:
        config = resolve_config(
            user="me", url="https://hub/mcp", pat="ghp", env={}
        )
        assert config.mode == "stateful"

    def test_mode_arg_passes_through(self) -> None:
        config = resolve_config(
            user="me",
            mode="stateless",
            url="https://hub/mcp",
            pat="ghp",
            env={},
        )
        assert config.mode == "stateless"


class TestMakeHeaders:
    """``make_headers`` produces the right MCP HTTP shape."""

    def test_required_headers(self) -> None:
        config = Config(
            user="me",
            display_name=None,
            mode="stateful",
            tenant=None,
            url="https://hub/mcp",
            pat="ghp_xxx",
        )
        headers = make_headers(config)
        assert headers["Authorization"] == "Bearer ghp_xxx"
        assert headers["X-User-Id"] == "me"
        # Tenant absent → no X-Tenant-Id header (= default tenant).
        assert "X-Tenant-Id" not in headers

    def test_tenant_header_emitted(self) -> None:
        config = Config(
            user="me",
            display_name=None,
            mode="stateful",
            tenant="kaz",
            url="https://hub/mcp",
            pat="ghp_xxx",
        )
        headers = make_headers(config)
        assert headers["X-Tenant-Id"] == "kaz"
