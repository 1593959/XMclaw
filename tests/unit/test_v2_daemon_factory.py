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
    _apply_env_overrides,
    _resolve_secret_placeholders,
    build_agent_from_config,
    build_llm_from_config,
    build_skill_runtime_from_config,
    build_tools_from_config,
    load_config,
)
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.openai import OpenAILLM
from xmclaw.providers.runtime import LocalSkillRuntime, ProcessSkillRuntime
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


# ── Epic #16 Phase 1: secrets-layer fallback for api_key ────────────────


@pytest.fixture(autouse=True)
def _isolate_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin both secret stores under tmp_path and clear host XMC_SECRET_* env.

    Made autouse so EVERY factory test sees a clean slate — the pre-
    Phase-1 "empty api_key returns None" assertions would otherwise
    silently flip green-to-red on a developer box that has a real
    ``llm.anthropic.api_key`` stored (Phase 2 encrypted store default),
    because the secrets-layer fallback resolves it behind the scenes.

    These tests exercise the build_llm_from_config → get_secret fallback,
    so they must NEVER touch the developer's real ``~/.xmclaw/secrets.json``
    or the Phase 2 ``~/.xmclaw.secret/`` Fernet store.
    """
    monkeypatch.setenv("XMC_SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("XMC_SECRET_DIR", str(tmp_path / ".xmclaw.secret"))
    import os as _os
    for key in list(_os.environ):
        if key.startswith("XMC_SECRET_") and key != "XMC_SECRET_DIR":
            monkeypatch.delenv(key, raising=False)


def test_empty_cfg_api_key_falls_back_to_secrets_file(
    _isolate_secrets: None,
) -> None:
    """``api_key: ""`` in config + stored secret → factory picks it up.

    This is the opt-in path users take to keep cleartext out of
    config.json: leave the field empty, run ``xmclaw config set-secret
    llm.anthropic.api_key``, and the daemon resolves it on startup.
    """
    from xmclaw.utils.secrets import set_secret

    set_secret("llm.anthropic.api_key", "sk-ant-from-file")
    llm = build_llm_from_config({
        "llm": {"anthropic": {"api_key": "", "default_model": "claude-x"}},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.api_key == "sk-ant-from-file"


def test_whitespace_cfg_api_key_falls_back_to_secrets(
    _isolate_secrets: None,
) -> None:
    """A whitespace-only literal is the classic "export FOO= "-style
    footgun; it must NOT shadow the secrets-layer lookup."""
    from xmclaw.utils.secrets import set_secret

    set_secret("llm.openai.api_key", "sk-openai-fallback")
    llm = build_llm_from_config({
        "llm": {"openai": {"api_key": "   "}},
    })
    assert isinstance(llm, OpenAILLM)
    assert llm.api_key == "sk-openai-fallback"


def test_missing_cfg_api_key_falls_back_to_secrets(
    _isolate_secrets: None,
) -> None:
    """No ``api_key`` key at all in the provider dict still resolves
    via the secrets layer (common when a user scaffolds a provider
    block and forgets the field entirely)."""
    from xmclaw.utils.secrets import set_secret

    set_secret("llm.anthropic.api_key", "sk-ant-scaffold")
    llm = build_llm_from_config({
        "llm": {"anthropic": {}},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.api_key == "sk-ant-scaffold"


def test_cfg_literal_still_wins_over_secrets(_isolate_secrets: None) -> None:
    """A non-empty literal in config.json is the user's explicit choice
    — the secrets fallback must NOT override it (no "surprise, your env
    var won" footgun)."""
    from xmclaw.utils.secrets import set_secret

    set_secret("llm.anthropic.api_key", "sk-ant-from-secrets")
    llm = build_llm_from_config({
        "llm": {"anthropic": {"api_key": "sk-ant-from-cfg"}},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.api_key == "sk-ant-from-cfg"


def test_env_var_override_reaches_factory(
    _isolate_secrets: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``XMC_SECRET_LLM_ANTHROPIC_API_KEY`` is the CI-friendly path:
    config.json has empty api_key, env var carries the real value."""
    monkeypatch.setenv("XMC_SECRET_LLM_ANTHROPIC_API_KEY", "sk-ant-from-env")
    llm = build_llm_from_config({
        "llm": {"anthropic": {"api_key": ""}},
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.api_key == "sk-ant-from-env"


def test_returns_none_when_cfg_empty_and_no_secret(
    _isolate_secrets: None,
) -> None:
    """No literal + no secret = echo mode (not a crash)."""
    assert build_llm_from_config({
        "llm": {"anthropic": {"api_key": ""}},
    }) is None


def test_secrets_fallback_respects_provider_order(
    _isolate_secrets: None,
) -> None:
    """When BOTH providers rely on secrets-layer fallback, the stable
    order (anthropic first) still applies."""
    from xmclaw.utils.secrets import set_secret

    set_secret("llm.anthropic.api_key", "sk-ant-via-secrets")
    set_secret("llm.openai.api_key", "sk-oai-via-secrets")
    llm = build_llm_from_config({
        "llm": {
            "anthropic": {"api_key": ""},
            "openai": {"api_key": ""},
        },
    })
    assert isinstance(llm, AnthropicLLM)
    assert llm.api_key == "sk-ant-via-secrets"


# ── Epic #16 Phase 2: ${secret:NAME} placeholder resolver ────────────────
#
# Unit tests drive `_resolve_secret_placeholders` directly with a fake
# resolver (no filesystem, no keyring). Integration into `load_config` is
# covered below so users actually see the end-to-end "write
# ${secret:foo} in config.json → daemon boots with the real value".


def _fake_resolver(table: dict[str, str]):
    """Return a resolver closure backed by an in-memory dict.

    Keeps these tests independent of the real secrets layer so they run
    fast and don't leak state across the suite.
    """
    def _lookup(name: str) -> str | None:
        return table.get(name)
    return _lookup


def test_placeholder_resolver_substitutes_whole_string() -> None:
    """``"${secret:x}"`` on its own → resolver value wins."""
    out = _resolve_secret_placeholders(
        {"llm": {"anthropic": {"api_key": "${secret:anthropic_prod}"}}},
        _resolver=_fake_resolver({"anthropic_prod": "sk-ant-resolved"}),
    )
    assert out["llm"]["anthropic"]["api_key"] == "sk-ant-resolved"


def test_placeholder_resolver_handles_dotted_names() -> None:
    """Names with dots (the recommended xmclaw convention) survive
    round-trip to the resolver unchanged."""
    out = _resolve_secret_placeholders(
        {"api_key": "${secret:llm.anthropic.api_key}"},
        _resolver=_fake_resolver({"llm.anthropic.api_key": "sk-dotted"}),
    )
    assert out["api_key"] == "sk-dotted"


def test_placeholder_resolver_recurses_through_nested_dicts() -> None:
    """Placeholders bury at any depth; resolver walks everything."""
    cfg = {
        "channels": {
            "slack": {"token": "${secret:slack_bot}"},
            "discord": {"webhook": "${secret:discord_hook}"},
        },
        "tools": {"github": {"token": "${secret:gh_pat}"}},
    }
    out = _resolve_secret_placeholders(
        cfg,
        _resolver=_fake_resolver({
            "slack_bot": "xoxb-abc",
            "discord_hook": "https://hook.example/123",
            "gh_pat": "ghp_xyz",
        }),
    )
    assert out["channels"]["slack"]["token"] == "xoxb-abc"
    assert out["channels"]["discord"]["webhook"] == "https://hook.example/123"
    assert out["tools"]["github"]["token"] == "ghp_xyz"


def test_placeholder_resolver_walks_lists_elementwise() -> None:
    """List entries are treated like dict values — each string is a
    candidate for substitution."""
    cfg = {"allowed_keys": ["${secret:prod}", "plain", "${secret:staging}"]}
    out = _resolve_secret_placeholders(
        cfg,
        _resolver=_fake_resolver({"prod": "P", "staging": "S"}),
    )
    assert out["allowed_keys"] == ["P", "plain", "S"]


def test_placeholder_resolver_leaves_non_strings_alone() -> None:
    """Numbers / bools / None / mixed nested types pass through
    untouched. Regression guard against a "walk everything and call
    str()" implementation that would turn ints into strings."""
    cfg = {
        "gateway": {"port": 9000, "tls": True, "cert": None},
        "evolution": {"enabled": False, "threshold": 0.85},
    }
    out = _resolve_secret_placeholders(cfg, _resolver=_fake_resolver({}))
    assert out == cfg


def test_placeholder_resolver_rejects_partial_substitution() -> None:
    """``"prefix-${secret:x}-suffix"`` does NOT match the anchored
    pattern — it's treated as a literal. This is by design: partial
    substitution invites escaping bugs in the exact place you want
    zero surprise (API keys / tokens)."""
    cfg = {"url": "https://api.example/${secret:token}"}
    out = _resolve_secret_placeholders(
        cfg,
        _resolver=_fake_resolver({"token": "xyz"}),
    )
    # Literal preserved; no substitution attempted.
    assert out["url"] == "https://api.example/${secret:token}"


def test_placeholder_resolver_raises_on_unresolvable() -> None:
    """Typo'd name / not-yet-set secret → ConfigError. Silent fallback
    to None would e.g. degrade an LLM key to echo-mode without warning."""
    cfg = {"api_key": "${secret:anthropic_prod}"}
    with pytest.raises(ConfigError) as exc_info:
        _resolve_secret_placeholders(cfg, _resolver=_fake_resolver({}))
    # Error message carries both the path and the name so users can fix
    # without guessing what field triggered it.
    msg = str(exc_info.value)
    assert "anthropic_prod" in msg
    assert "$.api_key" in msg
    # And a remediation hint pointing at the CLI.
    assert "xmclaw config set-secret" in msg


def test_placeholder_resolver_raises_on_malformed_placeholder() -> None:
    """Strings that LOOK like the syntax but violate the charset rule
    raise instead of silently passing through. A typo-protection
    measure — ``"${secret:}"`` / ``"${secret: foo }"`` should be loud."""
    for bad in ("${secret:}", "${secret: foo}", "${secret:with space}"):
        cfg = {"k": bad}
        with pytest.raises(ConfigError) as exc_info:
            _resolve_secret_placeholders(cfg, _resolver=_fake_resolver({}))
        assert "malformed secret placeholder" in str(exc_info.value)


def test_placeholder_resolver_preserves_empty_containers() -> None:
    """Empty dicts / lists are structurally significant (they mark a
    section as present but empty) — resolver must not drop them."""
    cfg = {"channels": {}, "tools": {"allowed_dirs": []}}
    out = _resolve_secret_placeholders(cfg, _resolver=_fake_resolver({}))
    assert out == {"channels": {}, "tools": {"allowed_dirs": []}}


def test_load_config_resolves_placeholders_end_to_end(
    _isolate_secrets: None, tmp_path: Path,
) -> None:
    """Full round-trip: config.json with placeholder → ``load_config``
    returns the resolved value. This is the user-facing deliverable."""
    from xmclaw.utils.secrets import set_secret

    set_secret("my_anthropic", "sk-ant-resolved-e2e")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"anthropic": {
            "api_key": "${secret:my_anthropic}",
            "default_model": "claude",
        }},
    }), encoding="utf-8")

    cfg = load_config(cfg_path, env={})
    assert cfg["llm"]["anthropic"]["api_key"] == "sk-ant-resolved-e2e"

    # And the downstream factory builds a real LLM from it — no
    # placeholder-in-api_key leaking all the way to the provider.
    llm = build_llm_from_config(cfg)
    assert isinstance(llm, AnthropicLLM)
    assert llm.api_key == "sk-ant-resolved-e2e"


def test_load_config_unresolved_placeholder_raises_with_path(
    _isolate_secrets: None, tmp_path: Path,
) -> None:
    """If the referenced secret is missing, ``load_config`` fails loudly
    — callers see the JSON path + the offending name."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "${secret:never_set}"}},
    }), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path, env={})
    msg = str(exc_info.value)
    assert "never_set" in msg
    # Nested path shows up so the user can grep their config.
    assert "llm" in msg and "anthropic" in msg and "api_key" in msg


def test_load_config_resolve_secrets_false_keeps_literal(
    _isolate_secrets: None, tmp_path: Path,
) -> None:
    """``resolve_secrets=False`` keeps the placeholder in place — useful
    for config export / migration tooling that must round-trip the file
    without leaking real credentials."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "${secret:whatever}"}},
    }), encoding="utf-8")

    cfg = load_config(cfg_path, env={}, resolve_secrets=False)
    assert cfg["llm"]["anthropic"]["api_key"] == "${secret:whatever}"


def test_load_config_env_override_runs_before_secret_resolution(
    _isolate_secrets: None, tmp_path: Path,
) -> None:
    """If an env override injects a ``${secret:X}`` placeholder, the
    resolver still sees and resolves it. Precedence: file → ENV →
    secret resolution."""
    from xmclaw.utils.secrets import set_secret

    set_secret("from_env_route", "sk-from-env-route")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "sk-literal", "default_model": "c"}},
    }), encoding="utf-8")

    cfg = load_config(
        cfg_path,
        env={"XMC__llm__anthropic__api_key": "${secret:from_env_route}"},
    )
    assert cfg["llm"]["anthropic"]["api_key"] == "sk-from-env-route"


# ── build_agent_from_config ──────────────────────────────────────────────


def test_build_agent_returns_none_when_no_llm() -> None:
    bus = InProcessEventBus()
    agent = build_agent_from_config({"llm": {}}, bus)
    assert agent is None


def test_build_agent_without_tools_section_still_gets_full_tools() -> None:
    """Permissions default to MAXIMUM: no 'tools' section means the
    agent gets the full BuiltinTools roster, NOT a tool-less shell.
    Users who want a sandbox opt in explicitly."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
    }, bus)
    assert agent is not None
    assert agent._tools is not None
    names = {s.name for s in agent._tools.list_tools()}
    assert {"file_read", "file_write", "list_dir",
            "bash", "web_fetch", "web_search"} <= names


def test_build_agent_with_tools_section_wires_builtin_tools(tmp_path: Path) -> None:
    """With a tools section + allowlist, the agent still gets the full
    roster -- the allowlist only restricts which PATHS the fs tools
    accept, not which tools are available."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
        "tools": {"allowed_dirs": [str(tmp_path)]},
    }, bus)
    assert agent is not None
    assert agent._tools is not None
    names = {s.name for s in agent._tools.list_tools()}
    assert {"file_read", "file_write", "list_dir",
            "bash", "web_fetch", "web_search"} <= names


def test_build_agent_uses_configured_agent_id() -> None:
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
        "agent_id": "my-custom-agent",
    }, bus)
    assert agent is not None
    assert agent._agent_id == "my-custom-agent"


def test_build_agent_default_max_hops_is_40() -> None:
    """B-190: bumped default from 20 → 40. Audit-style work calling
    many list_dir/file_read used to hit the cap silently."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
    }, bus)
    assert agent is not None
    assert agent._max_hops == 40


def test_build_agent_reads_max_hops_from_agent_block() -> None:
    """B-190: ``cfg.agent.max_hops`` overrides the default."""
    bus = InProcessEventBus()
    agent = build_agent_from_config({
        "llm": {"anthropic": {"api_key": "k"}},
        "agent": {"max_hops": 80},
    }, bus)
    assert agent is not None
    assert agent._max_hops == 80


def test_build_agent_max_hops_kwarg_wins_over_config() -> None:
    """Explicit kwarg (used by tests) bypasses the config lookup."""
    bus = InProcessEventBus()
    agent = build_agent_from_config(
        {
            "llm": {"anthropic": {"api_key": "k"}},
            "agent": {"max_hops": 80},
        },
        bus,
        max_hops=5,
    )
    assert agent is not None
    assert agent._max_hops == 5


def test_build_agent_max_hops_invalid_falls_back_to_default() -> None:
    """Garbage values (negative, non-numeric) silently default to 40
    so a hand-edited config can't brick the agent."""
    bus = InProcessEventBus()
    for bad in [-1, 0, "lots", None]:
        agent = build_agent_from_config({
            "llm": {"anthropic": {"api_key": "k"}},
            "agent": {"max_hops": bad},
        }, bus)
        assert agent is not None
        assert agent._max_hops == 40, f"bad value {bad!r} should fall back"


# ── build_tools_from_config ──────────────────────────────────────────────


def test_build_tools_defaults_to_full_access_when_no_tools_section() -> None:
    """No 'tools' section => full BuiltinTools, NOT None. Permissions
    default to MAXIMUM: a local agent the user installed is meant to
    read their files, run shell commands, and hit the network. The
    earlier 'deny everything by default' posture produced the
    'list my Desktop -> permission denied' failure that prompted this
    reversal."""
    for cfg in ({}, {"llm": {}}):
        tools = build_tools_from_config(cfg)
        assert isinstance(tools, BuiltinTools)
        names = {s.name for s in tools.list_tools()}
        # All tool families on by default.
        assert {"file_read", "file_write", "list_dir", "bash",
                "web_fetch", "web_search"} <= names


def test_build_tools_structural_error_when_not_a_dict() -> None:
    with pytest.raises(ConfigError, match="'tools' must be an object"):
        build_tools_from_config({"tools": "not a dict"})


def test_build_tools_empty_section_defaults_to_full_access() -> None:
    """``tools: {}`` (no keys) also gives full access -- the user opted
    in to a tools section but configured no restrictions, so nothing
    is restricted. No ConfigError."""
    tools = build_tools_from_config({"tools": {}})
    assert isinstance(tools, BuiltinTools)
    names = {s.name for s in tools.list_tools()}
    assert "bash" in names and "web_fetch" in names


def test_build_tools_empty_allowed_dirs_collapses_to_no_sandbox() -> None:
    """Empty list used to be an error; now it collapses to 'no sandbox'
    (same as omitting the key) -- too easy to trip over by accident."""
    tools = build_tools_from_config({"tools": {"allowed_dirs": []}})
    assert isinstance(tools, BuiltinTools)
    assert tools._allowed is None


def test_build_tools_refuses_non_list_allowed_dirs() -> None:
    with pytest.raises(ConfigError, match="must be a list"):
        build_tools_from_config({"tools": {"allowed_dirs": "/path"}})


def test_build_tools_refuses_non_string_entry() -> None:
    with pytest.raises(ConfigError, match="entries must be strings"):
        build_tools_from_config({"tools": {"allowed_dirs": ["/ok", 42]}})


def test_build_tools_happy_path_with_allowlist(tmp_path: Path) -> None:
    tools = build_tools_from_config({
        "tools": {"allowed_dirs": [str(tmp_path)]},
    })
    assert isinstance(tools, BuiltinTools)
    tool_names = {s.name for s in tools.list_tools()}
    # All six tools present (filesystem + bash + web).
    assert {"file_read", "file_write", "list_dir", "bash",
            "web_fetch", "web_search"} <= tool_names


def test_build_tools_honors_kill_switches() -> None:
    """``enable_bash: false`` and ``enable_web: false`` drop those tools
    from list_tools() so the LLM never even sees them as options."""
    tools = build_tools_from_config({
        "tools": {"enable_bash": False, "enable_web": False},
    })
    assert isinstance(tools, BuiltinTools)
    names = {s.name for s in tools.list_tools()}
    assert "bash" not in names
    assert "web_fetch" not in names
    assert "web_search" not in names
    # Filesystem tools still present.
    assert {"file_read", "file_write", "list_dir"} <= names


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
    data = load_config(p, env={})
    assert data["llm"]["anthropic"]["default_model"] == "m"


# ── _apply_env_overrides (Epic #6) ───────────────────────────────────────


def test_env_override_replaces_existing_key() -> None:
    cfg = {"llm": {"anthropic": {"api_key": "from-file"}}}
    out = _apply_env_overrides(
        cfg, env={"XMC__llm__anthropic__api_key": "from-env"},
    )
    assert out["llm"]["anthropic"]["api_key"] == "from-env"


def test_env_override_creates_deep_nested_key() -> None:
    cfg: dict = {}
    _apply_env_overrides(
        cfg, env={"XMC__llm__openai__default_model": "gpt-x"},
    )
    assert cfg["llm"]["openai"]["default_model"] == "gpt-x"


def test_env_override_ignores_non_prefixed_vars() -> None:
    cfg = {"existing": True}
    _apply_env_overrides(
        cfg,
        env={"PATH": "/usr/bin", "HOME": "/home/x", "NOT_XMC": "keep"},
    )
    assert cfg == {"existing": True}


def test_env_override_coerces_bools() -> None:
    cfg: dict = {}
    _apply_env_overrides(
        cfg,
        env={
            "XMC__tools__enable_bash": "true",
            "XMC__tools__enable_web": "false",
        },
    )
    assert cfg["tools"]["enable_bash"] is True
    assert cfg["tools"]["enable_web"] is False


def test_env_override_coerces_numbers() -> None:
    cfg: dict = {}
    _apply_env_overrides(
        cfg,
        env={
            "XMC__daemon__port": "8765",
            "XMC__llm__temperature": "0.25",
        },
    )
    assert cfg["daemon"]["port"] == 8765
    assert cfg["llm"]["temperature"] == 0.25


def test_env_override_coerces_null() -> None:
    cfg: dict = {"llm": {"anthropic": {"base_url": "http://x"}}}
    _apply_env_overrides(
        cfg, env={"XMC__llm__anthropic__base_url": "null"},
    )
    assert cfg["llm"]["anthropic"]["base_url"] is None


def test_env_override_keeps_unrecognised_strings_as_str() -> None:
    """Secret-looking values that don't parse as JSON stay as strings."""
    cfg: dict = {}
    _apply_env_overrides(
        cfg, env={"XMC__llm__anthropic__api_key": "sk-ant-abc123"},
    )
    assert cfg["llm"]["anthropic"]["api_key"] == "sk-ant-abc123"


def test_env_override_parses_json_array() -> None:
    cfg: dict = {}
    _apply_env_overrides(
        cfg,
        env={"XMC__tools__allowed_dirs": '["/tmp", "/var/work"]'},
    )
    assert cfg["tools"]["allowed_dirs"] == ["/tmp", "/var/work"]


def test_env_override_overwrites_scalar_parent_with_dict() -> None:
    """If a parent path is a scalar (e.g. misconfig), ENV wins."""
    cfg = {"llm": "was-a-string"}
    _apply_env_overrides(
        cfg, env={"XMC__llm__anthropic__api_key": "k"},
    )
    assert cfg["llm"] == {"anthropic": {"api_key": "k"}}


def test_env_override_segments_are_lowercased() -> None:
    """Shell convention is upper-case ENV; our keys are lower-case."""
    cfg: dict = {}
    _apply_env_overrides(cfg, env={"XMC__LLM__ANTHROPIC__API_KEY": "k"})
    assert cfg["llm"]["anthropic"]["api_key"] == "k"


def test_env_override_empty_path_ignored() -> None:
    """Bare prefix or trailing __ must not crash."""
    cfg: dict = {"keep": 1}
    _apply_env_overrides(
        cfg, env={"XMC__": "nope", "XMC__llm____key": "v"},
    )
    assert cfg["keep"] == 1
    assert cfg["llm"]["key"] == "v"


def test_load_config_applies_env_overrides(tmp_path: Path) -> None:
    """End-to-end: file value is overridden by ENV at load time."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "file-key", "default_model": "m"}},
    }), encoding="utf-8")
    data = load_config(
        p, env={"XMC__llm__anthropic__api_key": "env-key"},
    )
    assert data["llm"]["anthropic"]["api_key"] == "env-key"
    assert data["llm"]["anthropic"]["default_model"] == "m"  # untouched


# ── build_skill_runtime_from_config (Epic #3) ───────────────────────────


def test_build_runtime_defaults_to_local_when_section_missing() -> None:
    rt = build_skill_runtime_from_config({})
    assert isinstance(rt, LocalSkillRuntime)


def test_build_runtime_defaults_to_local_when_backend_unset() -> None:
    rt = build_skill_runtime_from_config({"runtime": {}})
    assert isinstance(rt, LocalSkillRuntime)


def test_build_runtime_explicit_local() -> None:
    rt = build_skill_runtime_from_config({"runtime": {"backend": "local"}})
    assert isinstance(rt, LocalSkillRuntime)


def test_build_runtime_explicit_process() -> None:
    rt = build_skill_runtime_from_config({"runtime": {"backend": "process"}})
    assert isinstance(rt, ProcessSkillRuntime)


def test_build_runtime_rejects_non_dict_section() -> None:
    with pytest.raises(ConfigError):
        build_skill_runtime_from_config({"runtime": "process"})


def test_build_runtime_rejects_non_string_backend() -> None:
    with pytest.raises(ConfigError):
        build_skill_runtime_from_config({"runtime": {"backend": 1}})


def test_build_runtime_rejects_unknown_backend() -> None:
    with pytest.raises(ConfigError) as exc:
        build_skill_runtime_from_config(
            {"runtime": {"backend": "docker"}},
        )
    # Error message surfaces the known set so the user can pick one.
    assert "local" in str(exc.value)
    assert "process" in str(exc.value)
