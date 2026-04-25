"""daemon/config.example.json must round-trip through the v2 factory.

This test catches the "example config ships with a stale v1 shape"
regression. Previously the example had a ``tools`` section with
``bash_timeout`` / ``sandbox_timeout`` / ``browser_headless`` keys --
none recognized by v2's ``build_tools_from_config``, which raises
ConfigError when ``allowed_dirs`` is missing. So anyone who copied the
example to config.json and ran ``xmclaw start`` got an echo-mode daemon
with no warning.

The fix: example now declares ``tools.allowed_dirs``. This test ensures
the example stays parseable AND produces a BuiltinTools provider once
we wire in a placeholder api_key.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.factory import (
    build_agent_from_config,
    build_llm_from_config,
    build_tools_from_config,
)
from xmclaw.providers.tool.builtin import BuiltinTools


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_CFG = REPO_ROOT / "daemon" / "config.example.json"


@pytest.fixture(autouse=True)
def _isolate_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin both secret stores under tmp_path.

    The "echo mode when no api_key" assertion below would otherwise
    flip depending on whether the developer has a real
    ``llm.anthropic.api_key`` / ``llm.openai.api_key`` stored in the
    Phase 2 encrypted Fernet store — the secrets-layer fallback would
    resolve it silently and the factory would return an LLM provider
    instead of None.
    """
    monkeypatch.setenv("XMC_SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("XMC_SECRET_DIR", str(tmp_path / ".xmclaw.secret"))
    for key in list(os.environ):
        if key.startswith("XMC_SECRET_") and key != "XMC_SECRET_DIR":
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def example_cfg() -> dict:
    return json.loads(EXAMPLE_CFG.read_text(encoding="utf-8"))


def test_example_config_is_valid_json(example_cfg: dict) -> None:
    """Trivially parsable. Catches the case where a stray edit breaks
    JSON syntax."""
    assert isinstance(example_cfg, dict)


def test_example_tools_section_matches_v2_schema(example_cfg: dict) -> None:
    """``build_tools_from_config`` must accept the example's tools section
    without raising. If it raises, the example is shipping with a
    schema that v2 doesn't know how to build -- the user would silently
    end up in echo mode."""
    tools = build_tools_from_config(example_cfg)
    # Example has "." as a single allowed dir -> BuiltinTools, not None.
    assert isinstance(tools, BuiltinTools), (
        f"expected BuiltinTools, got {type(tools).__name__}"
    )
    specs = tools.list_tools()
    names = {s.name for s in specs}
    assert "file_read" in names
    assert "file_write" in names


def test_example_without_api_key_yields_echo_mode(example_cfg: dict) -> None:
    """The example ships with empty api_keys. build_llm_from_config
    should return None (echo mode) rather than crashing. That posture
    is what we document: the user fills in keys, THEN it becomes live."""
    llm = build_llm_from_config(example_cfg)
    assert llm is None


def test_example_builds_full_agent_when_api_key_filled(
    example_cfg: dict,
) -> None:
    """With an api_key patched in, the factory must produce a full
    AgentLoop (not None), wired with BuiltinTools. This is the
    "after editing config.example.json -> config.json" happy path."""
    example_cfg["llm"]["anthropic"]["api_key"] = "sk-placeholder"
    agent = build_agent_from_config(example_cfg, InProcessEventBus())
    assert isinstance(agent, AgentLoop)
    assert agent._tools is not None
    # And the tools surface is what we expect.
    names = {s.name for s in agent._tools.list_tools()}
    assert {"file_read", "file_write"} <= names


def test_gateway_port_is_8765(example_cfg: dict) -> None:
    """CLAUDE.md documents port 8765; the example should agree."""
    assert example_cfg["gateway"]["port"] == 8765
