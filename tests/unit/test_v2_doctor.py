"""``xmclaw v2 doctor`` — unit tests for each diagnostic check."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xmclaw.cli.doctor import (
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
    # On POSIX, Path.write_text respects umask (typically 0o644 on
    # GitHub runners), which doctor correctly flags as loose. Tighten
    # to 0600 so this test exercises the happy path, not the
    # loose-perms path (which has its own dedicated test).
    if sys.platform != "win32":
        import os
        os.chmod(p, 0o600)
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
    # Since Epic #10 every built-in check always produces a result;
    # config-dependent checks self-report "skipped" rather than being
    # silently dropped — this keeps --json output's shape stable for
    # downstream consumers.
    names = [r.name for r in results]
    assert "config" in names
    llm_r = next(r for r in results if r.name == "llm")
    tools_r = next(r for r in results if r.name == "tools")
    assert not llm_r.ok
    assert "skipped" in llm_r.detail
    assert not tools_r.ok
    assert "skipped" in tools_r.detail


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


# ── Epic #10: pluggable registry ─────────────────────────────────────────


from xmclaw.cli.doctor_registry import (
    CheckResult as RegistryCheckResult,
    DoctorCheck,
    DoctorContext,
    DoctorRegistry,
    build_default_registry,
)


def _write_valid_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "llm": {"anthropic": {"api_key": "k", "default_model": "m"}},
    }), encoding="utf-8")
    return p


class _PassingCheck(DoctorCheck):
    id = "test_pass"
    name = "test_pass"

    def run(self, ctx: DoctorContext) -> RegistryCheckResult:
        return RegistryCheckResult(name=self.name, ok=True, detail="green")


class _FailingCheck(DoctorCheck):
    id = "test_fail"
    name = "test_fail"

    def run(self, ctx: DoctorContext) -> RegistryCheckResult:
        return RegistryCheckResult(
            name=self.name, ok=False, detail="red", advisory="fix me",
        )


class _CrashingCheck(DoctorCheck):
    id = "test_crash"
    name = "test_crash"

    def run(self, ctx: DoctorContext) -> RegistryCheckResult:
        raise RuntimeError("boom")


def test_registry_preserves_registration_order() -> None:
    reg = DoctorRegistry()
    reg.register(_PassingCheck())
    reg.register(_FailingCheck())
    names = [c.name for c in reg.checks()]
    assert names == ["test_pass", "test_fail"]


def test_registry_run_all_returns_one_result_per_check(tmp_path: Path) -> None:
    reg = DoctorRegistry()
    reg.register(_PassingCheck())
    reg.register(_FailingCheck())
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    assert [r.name for r in results] == ["test_pass", "test_fail"]
    assert [r.ok for r in results] == [True, False]


def test_registry_run_all_catches_crashing_check(tmp_path: Path) -> None:
    """A broken check must not take down the whole diagnosis."""
    reg = DoctorRegistry()
    reg.register(_CrashingCheck())
    reg.register(_PassingCheck())
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    assert len(results) == 2
    assert not results[0].ok
    assert "RuntimeError" in results[0].detail
    assert results[1].ok  # the passing check still ran


def test_default_registry_has_six_builtin_checks() -> None:
    """The built-in set: config, llm, tools, pairing, port, daemon."""
    reg = build_default_registry()
    ids = [c.id for c in reg.checks()]
    assert ids == ["config", "llm", "tools", "pairing", "port", "daemon"]


def test_default_registry_config_check_populates_ctx_cfg(tmp_path: Path) -> None:
    """ConfigCheck must cache the parsed dict so downstream checks use it."""
    reg = build_default_registry()
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    results = reg.run_all(ctx)
    assert ctx.cfg is not None
    assert ctx.cfg["llm"]["anthropic"]["api_key"] == "k"
    llm_result = next(r for r in results if r.name == "llm")
    assert llm_result.ok  # LLMCheck found the cached cfg


def test_default_registry_llm_skips_when_config_failed(tmp_path: Path) -> None:
    """If ConfigCheck fails, LLMCheck/ToolsCheck must not crash."""
    reg = build_default_registry()
    ctx = DoctorContext(
        config_path=tmp_path / "does_not_exist.json",
        probe_daemon=False,
    )
    results = reg.run_all(ctx)
    config_r = next(r for r in results if r.name == "config")
    llm_r = next(r for r in results if r.name == "llm")
    tools_r = next(r for r in results if r.name == "tools")
    assert not config_r.ok
    assert not llm_r.ok
    assert "skipped" in llm_r.detail
    assert not tools_r.ok  # same handling for tools


def test_daemon_check_respects_no_probe_flag(tmp_path: Path) -> None:
    reg = build_default_registry()
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    results = reg.run_all(ctx)
    daemon_r = next(r for r in results if r.name == "daemon")
    assert daemon_r.ok
    assert "skipped" in daemon_r.detail


def test_check_result_to_dict_is_json_serializable() -> None:
    r = RegistryCheckResult(
        name="x", ok=True, detail="d", advisory=None, fix_available=False,
    )
    payload = r.to_dict()
    # Must round-trip through json.
    assert json.loads(json.dumps(payload)) == payload


def test_run_doctor_still_returns_old_check_result_type(tmp_path: Path) -> None:
    """The legacy run_doctor() signature must stay source-compatible:
    every element is a xmclaw.cli.doctor.CheckResult (not the registry
    one). Callers that import from the old module keep working."""
    results = run_doctor(
        _write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    assert all(isinstance(r, CheckResult) for r in results)
    assert [r.name for r in results] == [
        "config", "llm", "tools", "pairing", "port 8765", "daemon",
    ]


def test_fix_default_is_noop(tmp_path: Path) -> None:
    """DoctorCheck.fix() default must return False (no auto-fix)."""
    check = _PassingCheck()
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    assert check.fix(ctx) is False


def test_discover_plugins_returns_empty_when_no_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no entry points registered, discover returns an empty
    error list and registry is unchanged."""
    reg = DoctorRegistry()

    class _Empty:
        def __iter__(self):
            return iter(())

    import importlib.metadata as im

    monkeypatch.setattr(im, "entry_points", lambda **_kw: _Empty())
    errors = reg.discover_plugins()
    assert errors == []
    assert reg.checks() == []
