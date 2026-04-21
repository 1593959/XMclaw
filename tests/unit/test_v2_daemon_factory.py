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
    load_config,
)
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.openai import OpenAILLM


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


def test_build_agent_with_llm_has_no_tools_in_phase_4_2() -> None:
    """Phase 4.2 keeps tools out of the factory — explicit decision noted
    in the factory module docstring. Phase 4.3 adds a tools section."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
    }, bus)
    assert agent is not None
    assert agent._tools is None


def test_build_agent_uses_configured_agent_id() -> None:
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
        "agent_id": "my-custom-agent",
    }, bus)
    assert agent is not None
    assert agent._agent_id == "my-custom-agent"


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
