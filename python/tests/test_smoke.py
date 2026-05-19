"""Smoke test for the M0 skeleton.

Just asserts the package imports and exposes its version. M1 will replace
this with real coverage of `AgentHub.connect`, `register`, `send`, etc.
"""

import re

import agent_hub_sdk


def test_version_string() -> None:
    assert isinstance(agent_hub_sdk.__version__, str)
    # SemVer-ish — `0.0.0` for now, real release tags later.
    assert re.match(r"^\d+\.\d+\.\d+", agent_hub_sdk.__version__)
