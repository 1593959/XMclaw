"""``xmclaw v2 doctor`` — unit tests for each diagnostic check."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xmclaw.cli.v2_doctor import (
    CheckResult,
    check_config_file,
    check_daemon_health,
    check_llm_configured,
    check_pairing_token,
    check_port_available,
    check_tools_configured,
    run_doctor,
)


# ── check_config_file ──────────────────────────────────────────────────


def test_config_missing_file_is_critical(tmp_path: Path) -> None:
    result, cfg = check_config_file(tmp_path / "nope.json")
    assert not result.ok
    assert cfg is None
    assert "not found" in result.detail
    assert result.advisory is not None


def test_config_invalid_json_is_critical(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    result, cfg = check_config_file(p)
    assert not result.ok
    assert cfg is None
    assert "invalid JSON" in result.detail


def test_config_root_not_object_is_critical(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    result, cfg = check_config_file(p)
    assert not result.ok
    assert cfg is None
    assert "object" in result.detail


def test_config_happy_path_returns_dict(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    payload = {"llm": {"anthropic": {"api_key": "k"}}}
    p.write_text(json.dumps(payload), encoding="utf-8")
    result, cfg = check_config_file(p)
    assert result.ok
    assert cfg == payload


# ── check_llm_configured ───────────────────────────────────────────────


def test_llm_no_section_is_critical() -> None:
    r = check_llm_configured({})
    assert not r.ok
    assert "no 'llm' section" in r.detail


def test_llm_non_dict_section_is_critical() -> None:
    r = check_llm_configured({"llm": "not a dict"})
    assert not r.ok


def test_llm_no_api_key_is_critical() -> None:
    r = check_llm_configured({
        "llm": {"anthropic": {"api_key": "", "default_model": "x"}},
    })
    assert not r.ok
    assert "no provider has api_key" in r.detail


def test_llm_happy_path_surfaces_provider_and_model() -> None:
    r = check_llm_configured({
        "llm": {"anthropic": {"api_key": "k", "default_model": "claude-haiku"}},
    })
    assert r.ok
    assert "anthropic" in r.detail
    assert "claude-haiku" in r.detail


def test_llm_prefers_first_configured_provider() -> None:
    """If both anthropic and openai have keys, the first (anthropic) wins —
    matches the build_llm_from_config preference."""
    r = check_llm_configured({
        "llm": {
            "anthropic": {"api_key": "a", "default_model": "ca"},
            "openai":    {"api_key": "b", "default_model": "cb"},
        },
    })
    assert r.ok
    assert "anthropic" in r.detail


# ── check_tools_configured ──────────────────────────────────────────────


def test_tools_absent_is_informational_not_error() -> None:
    """No tools section → LLM-only mode. Not a failure."""
    r = check_tools_configured({})
    assert r.ok
    assert "LLM-only" in r.detail


def test_tools_non_dict_is_critical() -> None:
    r = check_tools_configured({"tools": "not a dict"})
    assert not r.ok


def test_tools_missing_allowed_dirs_is_critical() -> None:
    r = check_tools_configured({"tools": {}})
    assert not r.ok
    assert "allowed_dirs missing" in r.detail


def test_tools_empty_allowed_dirs_is_critical() -> None:
    r = check_tools_configured({"tools": {"allowed_dirs": []}})
    assert not r.ok


def test_tools_existing_dirs_green(tmp_path: Path) -> None:
    r = check_tools_configured({"tools": {"allowed_dirs": [str(tmp_path)]}})
    assert r.ok
    assert r.advisory is None


def test_tools_missing_dirs_is_advisory_not_error(tmp_path: Path) -> None:
    """Paths that don't exist yet are surfaced as an advisory — the user
    might be about to create them. Doctor shouldn't block on this."""
    r = check_tools_configured({
        "tools": {"allowed_dirs": [str(tmp_path / "future_workspace")]},
    })
    assert r.ok
    assert r.advisory is not None
    assert "don't exist" in r.advisory


# ── check_pairing_token ────────────────────────────────────────────────


def test_pairing_missing_file_is_informational(tmp_path: Path) -> None:
    """Missing pairing file isn't an error — serve creates it on start.
    Doctor just reports the expected location."""
    r = check_pairing_token(tmp_path / "no_token_yet.txt")
    assert r.ok
    assert "not yet created" in r.detail


def test_pairing_empty_file_is_critical(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    r = check_pairing_token(p)
    assert not r.ok
    assert "empty" in r.detail


def test_pairing_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "tok.txt"
    p.write_text("a" * 64, encoding="utf-8")
    r = check_pairing_token(p)
    assert r.ok
    assert "64 chars" in r.detail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perm check")
def test_pairing_loose_perms_is_critical(tmp_path: Path) -> None:
    import os
    p = tmp_path / "tok.txt"
    p.write_text("a" * 64, encoding="utf-8")
    os.chmod(p, 0o644)  # world-readable
    r = check_pairing_token(p)
    assert not r.ok
    assert "loose perms" in r.detail


# ── check_port_available ───────────────────────────────────────────────


def test_port_free() -> None:
    # Use a high port that's almost certainly free.
    r = check_port_available("127.0.0.1", 54327)
    assert r.ok
    # Could be either "available" or "in use" depending on what else
    # is running; both are ok=True (doctor surfaces in-use as advisory).


def test_port_in_use_is_advisory_not_error() -> None:
    """Bind a socket, then have the doctor check — it should report
    in-use without crashing."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        port = s.getsockname()[1]
        r = check_port_available("127.0.0.1", port)
        assert r.ok  # advisory, not error
        assert "in use" in r.detail
    finally:
        s.close()


# ── check_daemon_health ────────────────────────────────────────────────


def test_daemon_not_running_is_informational() -> None:
    r = check_daemon_health("127.0.0.1", 54328)
    assert r.ok
    assert "not running" in r.detail


# ── run_doctor integration ─────────────────────────────────────────────


def test_run_doctor_with_valid_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "k", "default_model": "m"}},
    }), encoding="utf-8")
    # Redirect pairing token path to a tmpdir via env var (which
    # default_token_path honors).
    import os
    old_env = os.environ.get("XMC_V2_PAIRING_TOKEN_PATH")
    os.environ["XMC_V2_PAIRING_TOKEN_PATH"] = str(tmp_path / "pair.txt")
    try:
        results = run_doctor(
            cfg_path, port=54329, probe_daemon=False,
        )
    finally:
        if old_env is None:
            os.environ.pop("XMC_V2_PAIRING_TOKEN_PATH", None)
        else:
            os.environ["XMC_V2_PAIRING_TOKEN_PATH"] = old_env

    by_name = {r.name: r for r in results}
    assert by_name["config"].ok
    assert by_name["llm"].ok
    assert by_name["tools"].ok    # no tools section = LLM-only = ok
    assert by_name["pairing"].ok  # missing file = "will be created" = ok
    assert "port 54329" in by_name


def test_run_doctor_with_missing_config(tmp_path: Path) -> None:
    results = run_doctor(
        tmp_path / "nope.json", port=54330, probe_daemon=False,
    )
    # Config failure → later config-dependent checks shouldn't fire.
    names = [r.name for r in results]
    assert "config" in names
    assert "llm" not in names   # skipped when config fails
    # Pairing + port still run (they don't depend on config).


# ── render helper ──────────────────────────────────────────────────────


def test_render_ok_uses_ok_icon() -> None:
    line = CheckResult(name="x", ok=True, detail="fine").render()
    assert "[ok]" in line
    assert "fine" in line


def test_render_critical_fail_uses_cross() -> None:
    line = CheckResult(name="x", ok=False, detail="broken").render()
    assert "[x]" in line


def test_render_advisory_uses_warning_and_includes_advisory_text() -> None:
    line = CheckResult(
        name="x", ok=False, detail="iffy", advisory="try this",
    ).render()
    assert "[!]" in line
    assert "try this" in line


def test_render_uses_ascii_only_for_windows_gbk_locale() -> None:
    """The render output must survive encoding to GBK / latin-1 / any
    single-byte codec. This is a real Windows-default-locale scenario."""
    line = CheckResult(
        name="x", ok=False, detail="broken", advisory="try this",
    ).render()
    # Round-trips through ASCII — proves no non-ASCII chars leaked.
    line.encode("ascii")
