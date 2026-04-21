"""daemon.factory — config → AgentLoop unit tests.

Picks the first provider with a real api_key; returns None if none are
configured; raises ConfigError on STRUCTURAL problems (llm section is
not a dict, config file unreadable).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.factory import (
    ConfigError,
    build_agent_from_config,
    build_llm_from_config,
    build_tools_from_config,
    load_config,
)
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.openai import OpenAILLM
from xmclaw.providers.tool.builtin import BuiltinTools


# ── build_llm_from_config ────────────────────────────────────────────────


def test_returns_none_when_no_llm_section() -> None:
    assert build_llm_from_config({}) is None


def test_returns_none_when_llm_section_empty() -> None:
    assert build_llm_from_config({"llm": {}}) is None


def test_returns_none_when_provider_has_no_api_key() -> None:
    assert build_llm_from_config({
        "llm": {"anthropic": {"api_key": "", "default_model": "claude"}},
    }) is None


def test_returns_none_when_api_key_is_null() -> None:
    assert build_llm_from_config({
        "llm": {"openai": {"api_key": None}},
    }) is None


def test_structural_error_when_llm_not_a_dict() -> None:
    with pytest.raises(ConfigError, match="must be an object"):
        build_llm_from_config({"llm": "definitely not a dict"})


def test_builds_anthropic_when_only_anthropic_configured() -> None:
    llm = build_llm_from_config({
        "llm": {"anthropic": {
            "api_key": "sk-ant-test", "default_model": "claude-haiku-4-5",
        }},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.model == "claude-haiku-4-5"
    assert llm.api_key == "sk-ant-test"


def test_builds_openai_when_only_openai_configured() -> None:
    llm = build_llm_from_config({
        "llm": {"openai": {"api_key": "sk-oai", "default_model": "gpt-4.1"}},
    })
    assert isinstance(llm, OpenAILLM)
    assert llm.model == "gpt-4.1"


def test_prefers_anthropic_when_both_configured() -> None:
    """Provider selection order is deterministic (Anthropic first)."""
    llm = build_llm_from_config({
        "llm": {
            "anthropic": {"api_key": "a", "default_model": "ca"},
            "openai":    {"api_key": "b", "default_model": "cb"},
        },
    })
    assert isinstance(llm, AnthropicLLM)


def test_skips_provider_without_key_and_picks_next() -> None:
    llm = build_llm_from_config({
        "llm": {
            "anthropic": {"api_key": ""},  # no key
            "openai":    {"api_key": "yes", "default_model": "gpt-x"},
        },
    })
    assert isinstance(llm, OpenAILLM)
    assert llm.model == "gpt-x"


def test_base_url_plumbed_through() -> None:
    llm = build_llm_from_config({
        "llm": {"anthropic": {
            "api_key": "k", "default_model": "m",
            "base_url": "https://compat.example/anthropic",
        }},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.base_url == "https://compat.example/anthropic"


def test_falls_back_to_default_model_when_omitted() -> None:
    llm = build_llm_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.model  # non-empty default model


# ── build_agent_from_config ──────────────────────────────────────────────


def test_build_agent_returns_none_when_no_llm() -> None:
    bus = InProcessEventBus()
    agent = build_agent_from_config({"llm": {}}, bus)
    assert agent is None


def test_build_agent_without_tools_section_yields_toolless_agent() -> None:
    """No tools section → AgentLoop with tools=None (pure-chat mode)."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
    }, bus)
    assert agent is not None
    assert agent._tools is None


def test_build_agent_with_tools_section_wires_builtin_tools(tmp_path: Path) -> None:
    """Phase 4.3: tools section present → AgentLoop carries BuiltinTools."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
        "tools": {"allowed_dirs": [str(tmp_path)]},
    }, bus)
    assert agent is not None
    assert agent._tools is not None
    tool_names = {s.name for s in agent._tools.list_tools()}
    assert tool_names == {"file_read", "file_write"}


def test_build_agent_uses_configured_agent_id() -> None:
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
        "agent_id": "my-custom-agent",
    }, bus)
    assert agent is not None
    assert agent._agent_id == "my-custom-agent"


# ── build_tools_from_config ──────────────────────────────────────────────


def test_build_tools_returns_none_when_no_tools_section() -> None:
    assert build_tools_from_config({}) is None
    assert build_tools_from_config({"llm": {}}) is None


def test_build_tools_structural_error_when_not_a_dict() -> None:
    with pytest.raises(ConfigError, match="'tools' must be an object"):
        build_tools_from_config({"tools": "not a dict"})


def test_build_tools_refuses_missing_allowed_dirs() -> None:
    """Section present without allowed_dirs → explicit error. Tools must
    say where they can touch — no implicit 'trust the caller' via config."""
    with pytest.raises(ConfigError, match="allowed_dirs.*required"):
        build_tools_from_config({"tools": {}})


def test_build_tools_refuses_empty_allowed_dirs() -> None:
    """Empty list means 'deny everything' which makes tools enabled but
    unusable — a clear admin mistake. Refused."""
    with pytest.raises(ConfigError, match="must be non-empty"):
        build_tools_from_config({"tools": {"allowed_dirs": []}})


def test_build_tools_refuses_non_list_allowed_dirs() -> None:
    with pytest.raises(ConfigError, match="must be a list"):
        build_tools_from_config({"tools": {"allowed_dirs": "/path"}})


def test_build_tools_refuses_non_string_entry() -> None:
    with pytest.raises(ConfigError, match="entries must be strings"):
        build_tools_from_config({"tools": {"allowed_dirs": ["/ok", 42]}})


def test_build_tools_happy_path(tmp_path: Path) -> None:
    tools = build_tools_from_config({
        "tools": {"allowed_dirs": [str(tmp_path)]},
    })
    assert isinstance(tools, BuiltinTools)
    tool_names = {s.name for s in tools.list_tools()}
    assert tool_names == {"file_read", "file_write"}


@pytest.mark.asyncio
async def test_tools_enforce_configured_allowlist(tmp_path: Path) -> None:
    """End-to-end: config-built tools actually reject paths outside the
    allowlist at invocation time — the security posture is real, not
    just a config acknowledgment."""
    tools = build_tools_from_config({
        "tools": {"allowed_dirs": [str(tmp_path)]},
    })
    assert tools is not None

    outside = tmp_path.parent / "_outside_factory_test.txt"
    from xmclaw.core.ir import ToolCall
    result = await tools.invoke(ToolCall(
        name="file_write",
        args={"path": str(outside), "content": "should be blocked"},
        provenance="synthetic",
    ))
    try:
        assert result.ok is False
        assert "permission" in result.error.lower()
        assert not outside.exists()
    finally:
        if outside.exists():
            outside.unlink()


# ── load_config ──────────────────────────────────────────────────────────


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "no_such.json")


def test_load_config_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(p)


def test_load_config_root_must_be_object(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text('["nope"]', encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an object"):
        load_config(p)


def test_load_config_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "k", "default_model": "m"}},
    }), encoding="utf-8")
    data = load_config(p)
    assert data["llm"]["anthropic"]["default_model"] == "m"
