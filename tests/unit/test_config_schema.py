"""Tests for the static config schema validator (P1-4)."""
from __future__ import annotations

import pytest

from xmclaw.daemon.config_schema import validate_config, validate_or_raise
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
