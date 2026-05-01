"""B-78: ConfigDeadFieldsCheck — flag config.json keys that no
production code path consumes.

Pins:
  * Clean config (only known keys) → ok=True, "no unknown fields"
  * Ghost top-level key → flagged
  * Ghost child under a known top-level key → flagged with full path
  * Underscored keys (_comment etc) skipped — those are user notes
  * Permissive sections (mcp_servers / integrations) NOT recursed into —
    their children are user-named, anything goes
  * No config loaded → ok=True with "no config loaded" detail
"""
from __future__ import annotations


from xmclaw.cli.doctor_registry import (
    ConfigDeadFieldsCheck,
    DoctorContext,
)


def _ctx(cfg) -> DoctorContext:
    """Build a context with a pre-loaded cfg (skip the file-load path)."""
    from pathlib import Path

    ctx = DoctorContext(config_path=Path("/dev/null"), probe_daemon=False)
    ctx.cfg = cfg
    return ctx


def test_clean_config_returns_ok_no_unknown() -> None:
    cfg = {
        "llm": {"default_provider": "anthropic"},
        "tools": {"allowed_dirs": [], "enable_bash": True},
        "memory": {"enabled": True, "db_path": None},
    }
    res = ConfigDeadFieldsCheck().run(_ctx(cfg))
    assert res.ok
    assert "no unknown fields" in res.detail


def test_ghost_top_level_key_flagged() -> None:
    cfg = {
        "llm": {"default_provider": "anthropic"},
        "wat_is_this": {"foo": "bar"},
    }
    res = ConfigDeadFieldsCheck().run(_ctx(cfg))
    assert res.ok  # informational, not a hard fail
    assert "wat_is_this" in res.detail
    assert "1 unknown" in res.detail


def test_ghost_child_under_known_section_flagged_with_full_path() -> None:
    """The original incident: memory.vector_db_path / session_retention_days
    / max_context_tokens left over from an archived design doc."""
    cfg = {
        "memory": {
            "vector_db_path": "/some/path",
            "session_retention_days": 7,
            "max_context_tokens": 120000,
            "enabled": True,  # this one IS valid, must not be flagged
        },
    }
    res = ConfigDeadFieldsCheck().run(_ctx(cfg))
    assert res.ok
    assert "memory.vector_db_path" in res.detail
    assert "memory.session_retention_days" in res.detail
    assert "memory.max_context_tokens" in res.detail
    # The valid sibling is NOT in the ghost list.
    assert "memory.enabled" not in res.detail


def test_underscored_keys_are_skipped() -> None:
    cfg = {
        "_comment": "top-level note",
        "memory": {
            "_comment": "section note",
            "enabled": True,
        },
    }
    res = ConfigDeadFieldsCheck().run(_ctx(cfg))
    assert res.ok
    assert "no unknown fields" in res.detail


def test_permissive_sections_skip_child_walk() -> None:
    """mcp_servers / integrations have user-named children; we must not
    flag those as ghost keys."""
    cfg = {
        "mcp_servers": {
            "filesystem": {"command": "npx", "args": []},
            "any-name-the-user-picks": {"foo": "bar"},
        },
        "integrations": {
            "totally-fake-vendor": {"api_key": "x"},
        },
    }
    res = ConfigDeadFieldsCheck().run(_ctx(cfg))
    assert res.ok
    assert "no unknown fields" in res.detail


def test_no_config_loaded_returns_ok() -> None:
    ctx = _ctx(None)  # cfg explicitly None
    res = ConfigDeadFieldsCheck().run(ctx)
    assert res.ok
    assert "no config loaded" in res.detail


def test_typo_in_known_section_is_advisory() -> None:
    """Typos like 'tools.enable_bashh' (extra h) get caught."""
    cfg = {
        "tools": {"enable_bashh": True},
    }
    res = ConfigDeadFieldsCheck().run(_ctx(cfg))
    assert res.ok
    assert "tools.enable_bashh" in res.detail
    assert res.advisory is not None
