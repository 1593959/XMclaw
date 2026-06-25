"""Tests for the static config schema validator (P1-4)."""
from __future__ import annotations

import pytest

from xmclaw.daemon.config_schema import lint_config, validate_config, validate_or_raise
from xmclaw.daemon.factory import ConfigError


# ─── happy path ───────────────────────────────────────────────────


def test_empty_config_is_valid():
    """Empty dict is valid — every key is optional at this layer."""
    assert validate_config({}) == []


def test_well_shaped_config_is_valid():
    cfg = {
        "llm": {
            "profiles": [
                {"id": "p1", "model": "x", "supports_vision": True},
                {"id": "p2", "model": "y"},
            ],
        },
        "gateway": {"host": "127.0.0.1", "port": 8766},
        "cognition": {
            "continuous_loop": {
                "autonomy_level": 50,
                "heartbeat_hz": 1.0,
            },
            "auto_recall": {
                "enabled": False,
                "use_hybrid": False,
                "environment_enabled": True,
                "timeout_s": 1.0,
                "min_similarity": 0.65,
            },
            "memory_v2": {
                "retention": {
                    "sweep_interval_s": 3600,
                    "dedup_every_n_sweeps": 24,
                    "dedup_scopes": ["user", "project"],
                },
            },
        },
        "evolution": {"enabled": True, "auto_apply": True},
    }
    assert validate_config(cfg) == []


# ─── gateway.port ─────────────────────────────────────────────────


def test_port_out_of_range_rejected():
    errs = validate_config({"gateway": {"port": 99999}})
    assert any("gateway.port" in e and "65535" in e for e in errs)


def test_port_negative_rejected():
    errs = validate_config({"gateway": {"port": -1}})
    assert any("gateway.port" in e for e in errs)


def test_port_wrong_type_rejected():
    errs = validate_config({"gateway": {"port": "8766"}})  # string, not int
    assert any("gateway.port" in e and "expected int" in e for e in errs)


# ─── autonomy_level ───────────────────────────────────────────────


def test_autonomy_above_100_rejected():
    errs = validate_config({
        "cognition": {"continuous_loop": {"autonomy_level": 150}},
    })
    assert any("autonomy_level" in e and "[0, 100]" in e for e in errs)


def test_autonomy_negative_rejected():
    errs = validate_config({
        "cognition": {"continuous_loop": {"autonomy_level": -10}},
    })
    assert any("autonomy_level" in e for e in errs)


def test_autonomy_string_rejected():
    """LLM-style boolean coercion would silently accept '50' — schema
    must reject the type mismatch."""
    errs = validate_config({
        "cognition": {"continuous_loop": {"autonomy_level": "50"}},
    })
    assert any("autonomy_level" in e and "expected int" in e for e in errs)


# ─── evolution.enabled / auto_apply ───────────────────────────────


def test_evolution_auto_apply_string_rejected():
    """JSON 'true'/'false' as STRINGS look fine to a human but break
    the bool reader."""
    errs = validate_config({"evolution": {"auto_apply": "true"}})
    assert any("evolution.auto_apply" in e for e in errs)


# ─── auto_recall block ────────────────────────────────────────────


def test_auto_recall_timeout_zero_rejected():
    errs = validate_config({
        "cognition": {"auto_recall": {"timeout_s": 0}},
    })
    assert any("auto_recall.timeout_s" in e and "> 0" in e for e in errs)


def test_auto_recall_similarity_out_of_unit_interval():
    errs = validate_config({
        "cognition": {"auto_recall": {"min_similarity": 1.5}},
    })
    assert any("min_similarity" in e and "[0.0, 1.0]" in e for e in errs)


def test_auto_recall_enabled_wrong_type():
    errs = validate_config({
        "cognition": {"auto_recall": {"enabled": "yes"}},
    })
    assert any(
        "auto_recall.enabled" in e and "expected bool" in e for e in errs
    )


def test_auto_recall_environment_enabled_wrong_type():
    errs = validate_config({
        "cognition": {"auto_recall": {"environment_enabled": "yes"}},
    })
    assert any(
        "auto_recall.environment_enabled" in e and "expected bool" in e
        for e in errs
    )


# ─── memory_v2.retention ──────────────────────────────────────────


def test_dedup_scopes_must_be_list_of_strings():
    errs = validate_config({
        "cognition": {
            "memory_v2": {
                "retention": {"dedup_scopes": [1, 2, 3]},
            },
        },
    })
    assert any("dedup_scopes" in e for e in errs)


def test_sweep_interval_negative_rejected():
    errs = validate_config({
        "cognition": {
            "memory_v2": {"retention": {"sweep_interval_s": -1}},
        },
    })
    assert any("sweep_interval_s" in e for e in errs)


# ─── llm.profiles ─────────────────────────────────────────────────


def test_duplicate_profile_id_rejected():
    errs = validate_config({
        "llm": {
            "profiles": [
                {"id": "p1", "model": "a"},
                {"id": "p1", "model": "b"},
            ],
        },
    })
    assert any("duplicate id" in e for e in errs)


def test_missing_profile_id_rejected():
    errs = validate_config({
        "llm": {"profiles": [{"model": "x"}]},  # no id
    })
    assert any("profiles[0].id" in e for e in errs)


def test_supports_vision_must_be_bool():
    errs = validate_config({
        "llm": {
            "profiles": [
                {"id": "p1", "model": "x", "supports_vision": "true"},
            ],
        },
    })
    assert any("supports_vision" in e for e in errs)


# ─── multi-error aggregation ──────────────────────────────────────


def test_multiple_problems_reported_together():
    """User shouldn't have to fix-restart-fix-restart. Report ALL
    problems on the first pass."""
    errs = validate_config({
        "gateway": {"port": 999999},
        "cognition": {
            "continuous_loop": {"autonomy_level": 200},
            "auto_recall": {"timeout_s": -1},
        },
        "evolution": {"enabled": "yes"},
    })
    assert len(errs) >= 4


# ─── validate_or_raise integration ────────────────────────────────


def test_validate_or_raise_passes_clean_config():
    """Valid config returns silently."""
    validate_or_raise({"gateway": {"port": 8766}})  # no exception


def test_validate_or_raise_raises_config_error_with_paths():
    """All bad paths surface in the single error message."""
    with pytest.raises(ConfigError) as excinfo:
        validate_or_raise({
            "gateway": {"port": -1},
            "cognition": {"continuous_loop": {"autonomy_level": 999}},
        })
    msg = str(excinfo.value)
    assert "gateway.port" in msg
    assert "autonomy_level" in msg
    # The summary line tells the user there are N problems.
    assert "2 problems" in msg or "validation failed" in msg


# ─── lint_config extended checks ──────────────────────────────────


def test_lint_config_returns_config_errors():
    """lint_config wraps every problem in a ConfigError instance."""
    errs = lint_config({"gateway": {"port": 99999}})
    assert all(isinstance(e, ConfigError) for e in errs)
    assert any("gateway.port" in str(e) for e in errs)


def test_lint_config_url_validation():
    errs = lint_config({"llm": {"openai": {"base_url": "not-a-url"}}})
    assert any("llm.openai.base_url" in str(e) and "URL" in str(e) for e in errs)


def test_lint_config_model_whitelist():
    errs = lint_config({"llm": {"openai": {"default_model": "totally-unknown-model-xyz"}}})
    assert any("unknown model name" in str(e) for e in errs)


def test_lint_config_valid_model_passes():
    assert lint_config({"llm": {"openai": {"default_model": "gpt-4o"}}}) == []


def test_lint_config_ollama_model_format():
    assert lint_config({"llm": {"openai": {"default_model": "ollama/llama3"}}}) == []


def test_lint_config_temperature_range():
    errs = lint_config({"llm": {"openai": {"temperature": 3.0}}})
    assert any("temperature" in str(e) and "[0.0, 2.0]" in str(e) for e in errs)


def test_lint_config_max_tokens_range():
    errs = lint_config({"llm": {"openai": {"max_tokens": 200000}}})
    assert any("max_tokens" in str(e) and "[1, 128000]" in str(e) for e in errs)


def test_lint_config_agent_max_hops_range():
    errs = lint_config({"agent": {"max_hops": 0}})
    assert any("agent.max_hops" in str(e) for e in errs)


def test_lint_config_agent_max_react_loop_range():
    errs = lint_config({"agent": {"max_react_loop": 0}})
    assert any("agent.max_react_loop" in str(e) for e in errs)


def test_lint_config_tools_invoke_timeout_range():
    errs = lint_config({"tools": {"invoke_timeout_s": 0}})
    assert any("tools.invoke_timeout_s" in str(e) for e in errs)


def test_lint_config_typed_overlay_covers_continuous_loop_and_tool_timeout():
    errs = lint_config({
        "tools": {"invoke_timeout_s": 601},
        "cognition": {
            "continuous_loop": {
                "autonomy_level": 101,
                "heartbeat_hz": 0,
            },
        },
    })

    joined = "\n".join(str(e) for e in errs)
    assert "tools.invoke_timeout_s" in joined
    assert "cognition.continuous_loop.autonomy_level" in joined
    assert "cognition.continuous_loop.heartbeat_hz" in joined


def test_lint_config_tools_shell_execution_policy_values():
    assert lint_config({"tools": {"shell": {"execution_policy": "docker"}}}) == []
    errs = lint_config({"tools": {"shell": {"execution_policy": "mystery"}}})
    assert any("tools.shell.execution_policy" in str(e) for e in errs)


def test_lint_config_tools_shell_sandbox_image_string():
    assert lint_config({"tools": {"shell": {"sandbox_image": "alpine:3.20"}}}) == []
    errs = lint_config({"tools": {"shell": {"sandbox_image": 123}}})
    assert any("tools.shell.sandbox_image" in str(e) for e in errs)


def test_lint_config_tools_shell_sandbox_resource_values():
    assert lint_config({
        "tools": {
            "shell": {
                "sandbox_memory": "1g",
                "sandbox_cpus": "0.5",
                "sandbox_pids_limit": 64,
                "sandbox_network": "bridge",
            },
        },
    }) == []
    errs = lint_config({
        "tools": {
            "shell": {
                "sandbox_memory": 1,
                "sandbox_cpus": 1,
                "sandbox_pids_limit": 1,
                "sandbox_network": "host",
            },
        },
    })
    joined = "\n".join(str(e) for e in errs)
    assert "tools.shell.sandbox_memory" in joined
    assert "tools.shell.sandbox_cpus" in joined
    assert "tools.shell.sandbox_pids_limit" in joined
    assert "tools.shell.sandbox_network" in joined


def test_lint_config_self_critique_enabled_bool():
    assert lint_config({"cognition": {"self_critique": {"enabled": True}}}) == []
    errs = lint_config({"cognition": {"self_critique": {"enabled": "yes"}}})
    assert any("cognition.self_critique.enabled" in str(e) for e in errs)


def test_lint_config_memory_v2_dependency():
    errs = lint_config({"cognition": {"memory_v2": {"enabled": True}}})
    assert any("lancedb_uri" in str(e) for e in errs)


def test_lint_config_swarm_dependency():
    errs = lint_config({"swarm": {"enabled": True, "max_subagents": 1}})
    assert any("swarm" in str(e) and "max_subagents" in str(e) for e in errs)


def test_lint_config_evolution_local_model():
    errs = lint_config({
        "evolution": {"enabled": True},
        "llm": {"openai": {"default_model": "ollama/llama-3-8b"}},
    })
    assert any("local small model" in str(e) for e in errs)


def test_lint_config_empty_is_valid():
    assert lint_config({}) == []
