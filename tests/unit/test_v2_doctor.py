"""``xmclaw v2 doctor`` — unit tests for each diagnostic check."""
from __future__ import annotations

import json
import os
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
from xmclaw.cli.doctor_registry import (
    CheckResult as RegistryCheckResult,
    ConnectivityCheck,
    DoctorCheck,
    DoctorContext,
    DoctorRegistry,
    EventsDbCheck,
    WorkspaceCheck,
    build_default_registry,
)


# ── check_config_file ──────────────────────────────────────────────────


def test_config_missing_file_is_critical(tmp_path: Path) -> None:
    result, cfg = check_config_file(tmp_path / "nope.json")
    assert not result.ok
    assert cfg is None
    assert "not found" in result.detail
    assert result.advisory is not None
    # Advisory should point at the working command, not stale copy-paste instructions.
    assert "config init" in result.advisory


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


def test_default_registry_builtin_check_order() -> None:
    """Built-in set and their order.

    Order matters: ConfigCheck must run first so ctx.cfg is cached for
    downstream checks. RoadmapLintCheck is late because it's a
    doc-consistency guard, not a runtime blocker.
    """
    reg = build_default_registry()
    ids = [c.id for c in reg.checks()]
    assert ids == [
        "config", "llm", "tools", "workspace", "pairing", "port",
        "events_db", "memory_db", "skill_runtime",
        "connectivity", "roadmap_lint", "pid_lock", "daemon",
        "backups", "secrets",
    ]


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
        "config", "llm", "tools", "workspace", "pairing", "port 8765",
        "events_db", "memory_db", "skill_runtime",
        "connectivity", "roadmap_lint", "pid_lock", "daemon",
        "backups", "secrets",
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


# ── Epic #10 exit standard: 第三方 pilot 插件可注册自检 ───────────────────
#
# discover_plugins() 走 importlib.metadata entry_points。我们不去装真包，
# 直接 monkeypatch entry_points 回一个合成的 FakeEP，验证端到端路径：
#   1. class 形态 entry-point → registry 吸入 → run_all 出结果
#   2. factory callable 形态 entry-point → 同样吸入
#   3. 损坏 entry-point（import 炸 / 构造炸 / resolve 到非 DoctorCheck）
#      不可让 doctor 整体停机，要返回 synthetic failure CheckResult
#
# 这组用例共同证明 doctor 对外是开放的插件面，而非仅内部 15 条硬编码。


class _FakeEP:
    """Mimics an importlib.metadata EntryPoint closely enough for
    discover_plugins(): .name / .value attributes + .load() callable."""

    def __init__(self, name: str, target, value: str = "<pilot>"):
        self.name = name
        self.value = value
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _install_fake_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    eps: list,
) -> None:
    """Force ``importlib.metadata.entry_points(group="xmclaw.doctor")`` to
    return ``eps`` regardless of the host's installed packages."""
    import importlib.metadata as im

    def _fake(**kwargs):
        # discover_plugins() calls entry_points(group=...) on py3.10+.
        if kwargs.get("group") == "xmclaw.doctor":
            return eps
        return []

    monkeypatch.setattr(im, "entry_points", _fake)


class _PluginPassingCheck(DoctorCheck):
    """Pilot plugin check — mirrors a minimal third-party impl."""
    id = "pilot_green"
    name = "pilot_green"

    def run(self, ctx: DoctorContext) -> RegistryCheckResult:
        return RegistryCheckResult(
            name=self.name, ok=True, detail="pilot reporting green",
        )


def _pilot_factory() -> DoctorCheck:
    """Factory variant — entry-point resolves to a callable that
    returns a DoctorCheck instance. Plugin authors may prefer this
    when they need to read config at construction time."""
    return _PluginPassingCheck()


def test_discover_plugins_registers_class_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third-party pilot registering a DoctorCheck subclass as its
    entry-point target is loaded and runs alongside built-ins."""
    reg = DoctorRegistry()
    _install_fake_entry_points(monkeypatch, [
        _FakeEP("pilot", _PluginPassingCheck),
    ])

    errors = reg.discover_plugins()
    assert errors == []
    # Plugin check now sits in the registry ready to run.
    names = [c.name for c in reg.checks()]
    assert "pilot_green" in names

    # Full run: plugin check result appears with the others.
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    hit = [r for r in results if r.name == "pilot_green"]
    assert len(hit) == 1
    assert hit[0].ok is True
    assert hit[0].detail == "pilot reporting green"


def test_discover_plugins_registers_factory_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A factory-shaped entry-point (callable returning a DoctorCheck
    instance) is equally valid."""
    reg = DoctorRegistry()
    _install_fake_entry_points(monkeypatch, [
        _FakeEP("pilot_factory", _pilot_factory),
    ])

    errors = reg.discover_plugins()
    assert errors == []
    assert [c.name for c in reg.checks()] == ["pilot_green"]


def test_discover_plugins_surfaces_import_failure_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin whose ``load()`` raises must NOT take the whole doctor
    pass down — discover_plugins returns a synthetic failure entry so
    the user sees which package is broken."""
    reg = DoctorRegistry()
    boom = ImportError("module 'badplugin' not found")
    _install_fake_entry_points(monkeypatch, [
        _FakeEP("badplugin", boom, value="badplugin.doctor:Check"),
    ])

    errors = reg.discover_plugins()
    assert len(errors) == 1
    assert errors[0].name == "plugin:badplugin"
    assert errors[0].ok is False
    assert "failed to import" in errors[0].detail
    assert "ImportError" in errors[0].detail
    # The broken plugin itself did NOT land in the registry.
    assert reg.checks() == []


def test_discover_plugins_surfaces_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DoctorCheck subclass whose ``__init__`` raises lands as a
    synthetic failure, not a hard crash."""

    class _BadCtor(DoctorCheck):
        id = "bad_ctor"
        name = "bad_ctor"

        def __init__(self) -> None:
            raise RuntimeError("cannot construct")

        def run(self, ctx: DoctorContext) -> RegistryCheckResult:  # noqa: ARG002
            return RegistryCheckResult(name=self.name, ok=True)

    reg = DoctorRegistry()
    _install_fake_entry_points(monkeypatch, [
        _FakeEP("bad_ctor", _BadCtor),
    ])

    errors = reg.discover_plugins()
    assert len(errors) == 1
    assert errors[0].name == "plugin:bad_ctor"
    assert errors[0].ok is False
    assert "constructor raised" in errors[0].detail
    assert reg.checks() == []


def test_discover_plugins_rejects_wrong_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry-point resolving to something that is neither a
    DoctorCheck subclass nor a factory producing one is refused with
    a clear diagnostic."""
    reg = DoctorRegistry()
    _install_fake_entry_points(monkeypatch, [
        _FakeEP("wrong_type", "just a string", value="pkg:NOT_A_CHECK"),
    ])

    errors = reg.discover_plugins()
    assert len(errors) == 1
    assert errors[0].name == "plugin:wrong_type"
    assert errors[0].ok is False
    assert "did not resolve to a DoctorCheck" in errors[0].detail
    assert reg.checks() == []


def test_discover_plugins_isolates_failures_from_healthy_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken plugin does not prevent healthy plugins on the same
    entry-point group from registering. One synthetic failure + one
    real check should both land."""
    reg = DoctorRegistry()
    _install_fake_entry_points(monkeypatch, [
        _FakeEP("bad", ImportError("no such module")),
        _FakeEP("good", _PluginPassingCheck),
    ])

    errors = reg.discover_plugins()
    assert len(errors) == 1
    assert errors[0].name == "plugin:bad"
    # Healthy sibling still registered.
    assert [c.name for c in reg.checks()] == ["pilot_green"]

    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    names = [r.name for r in reg.run_all(ctx)]
    assert "pilot_green" in names


# ── Epic #10 phase 2: WorkspaceCheck + run_fixes ─────────────────────────


def _workspace_ctx(tmp_path: Path, workspace: Path) -> DoctorContext:
    """Minimal context pointed at an isolated tmp workspace."""
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    ctx.extras["workspace_dir"] = workspace
    return ctx


def test_workspace_missing_is_fixable(tmp_path: Path) -> None:
    ws = tmp_path / "v2_workspace"  # doesn't exist
    check = WorkspaceCheck()
    r = check.run(_workspace_ctx(tmp_path, ws))
    assert not r.ok
    assert r.fix_available is True
    assert "not found" in r.detail


def test_workspace_path_is_a_file_is_not_fixable(tmp_path: Path) -> None:
    """If something non-directory sits at the target path, we won't
    clobber it — that might be the user's own data."""
    ws = tmp_path / "v2_workspace"
    ws.write_text("a pre-existing file where a dir is expected")
    check = WorkspaceCheck()
    r = check.run(_workspace_ctx(tmp_path, ws))
    assert not r.ok
    assert r.fix_available is False
    assert "not a directory" in r.detail


def test_workspace_ready_returns_ok(tmp_path: Path) -> None:
    ws = tmp_path / "v2_workspace"
    ws.mkdir()
    check = WorkspaceCheck()
    r = check.run(_workspace_ctx(tmp_path, ws))
    assert r.ok
    assert r.fix_available is False
    assert "ready" in r.detail


def test_workspace_fix_creates_missing_dir(tmp_path: Path) -> None:
    ws = tmp_path / "nested" / "v2_workspace"  # parent also missing
    check = WorkspaceCheck()
    ctx = _workspace_ctx(tmp_path, ws)
    # Fix creates directory tree; re-running returns ok.
    assert check.fix(ctx) is True
    assert ws.is_dir()
    r = check.run(ctx)
    assert r.ok


def test_workspace_fix_is_idempotent_on_ready_dir(tmp_path: Path) -> None:
    ws = tmp_path / "v2_workspace"
    ws.mkdir()
    check = WorkspaceCheck()
    assert check.fix(_workspace_ctx(tmp_path, ws)) is True


def test_workspace_fix_refuses_to_replace_a_file(tmp_path: Path) -> None:
    """fix() must not clobber a file at the target path."""
    ws = tmp_path / "v2_workspace"
    ws.write_text("do not overwrite me")
    check = WorkspaceCheck()
    assert check.fix(_workspace_ctx(tmp_path, ws)) is False
    assert ws.is_file()  # untouched


# ── EventsDbCheck ────────────────────────────────────────────────────────


def _events_ctx(tmp_path: Path, db: Path) -> DoctorContext:
    ctx = DoctorContext(config_path=tmp_path / "unused.json")
    ctx.extras["events_db_path"] = db
    return ctx


def test_events_db_missing_file_is_ok(tmp_path: Path) -> None:
    """Daemon hasn't run yet — that's not a failure, just a note."""
    check = EventsDbCheck()
    r = check.run(_events_ctx(tmp_path, tmp_path / "events.db"))
    assert r.ok is True
    assert "not yet created" in r.detail


def test_events_db_path_is_a_directory_fails(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    db.mkdir()
    check = EventsDbCheck()
    r = check.run(_events_ctx(tmp_path, db))
    assert r.ok is False
    assert "not a file" in r.detail


def test_events_db_garbage_file_reports_parse_error(tmp_path: Path) -> None:
    """A non-SQLite file at the db path must fail parse — don't pretend."""
    db = tmp_path / "events.db"
    db.write_bytes(b"this is not a sqlite database, please fail me")
    check = EventsDbCheck()
    r = check.run(_events_ctx(tmp_path, db))
    assert r.ok is False
    assert "malformed" in r.detail or "cannot open" in r.detail


def test_events_db_healthy_current_schema_returns_ok(tmp_path: Path) -> None:
    """A DB at the current schema version should be green."""
    from xmclaw.core.bus.sqlite import SCHEMA_VERSION
    import sqlite3

    db = tmp_path / "events.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.close()
    check = EventsDbCheck()
    r = check.run(_events_ctx(tmp_path, db))
    assert r.ok is True
    assert f"v{SCHEMA_VERSION}" in r.detail


def test_events_db_newer_schema_fails_with_advisory(tmp_path: Path) -> None:
    """Downgrade isn't supported; surface it clearly rather than crash."""
    from xmclaw.core.bus.sqlite import SCHEMA_VERSION
    import sqlite3

    db = tmp_path / "events.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.close()
    check = EventsDbCheck()
    r = check.run(_events_ctx(tmp_path, db))
    assert r.ok is False
    assert "newer than code" in r.detail
    assert r.advisory is not None


# ── ConnectivityCheck ────────────────────────────────────────────────────


def _connectivity_ctx(
    tmp_path: Path, *, probe_network: bool, cfg: dict | None,
) -> DoctorContext:
    ctx = DoctorContext(
        config_path=tmp_path / "unused.json",
        probe_network=probe_network,
    )
    ctx.cfg = cfg
    return ctx


def test_connectivity_off_by_default_returns_ok(tmp_path: Path) -> None:
    """With ``probe_network=False`` the check must return ok without
    touching the network — the default doctor run has to stay offline-safe."""
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=False, cfg={
        "llm": {"anthropic": {"api_key": "k"}},
    }))
    assert r.ok is True
    assert "skipped" in r.detail.lower() or "--network" in r.detail


def test_connectivity_no_cfg_fails_cleanly(tmp_path: Path) -> None:
    """If ConfigCheck didn't populate ``ctx.cfg``, the connectivity check
    must report that and bail — not crash with an AttributeError."""
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg=None))
    assert r.ok is False
    assert "skipped" in r.detail


def test_connectivity_no_llm_section_is_ok(tmp_path: Path) -> None:
    """Probing something that isn't configured isn't a failure."""
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg={}))
    assert r.ok is True
    assert "nothing to probe" in r.detail


def test_connectivity_no_api_keys_is_ok(tmp_path: Path) -> None:
    """An LLM section with provider blocks but no api_keys isn't
    reachable-from-here — the user either forgot a key or this is a
    partial config. Either way, nothing to probe."""
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg={
        "llm": {"anthropic": {"default_model": "m"}},
    }))
    assert r.ok is True
    assert "nothing to probe" in r.detail


def test_connectivity_reachable_returns_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock urlopen to simulate a healthy TLS handshake."""
    import urllib.request

    class _FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    def _fake_urlopen(req, timeout):  # noqa: ARG001
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg={
        "llm": {"anthropic": {"api_key": "k"}},
    }))
    assert r.ok is True
    assert "reachable" in r.detail
    assert "HTTP 200" in r.detail


def test_connectivity_http_4xx_treated_as_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401/403 means the TLS handshake worked — we care about the
    network path, not auth. That's the LLMCheck's job."""
    import urllib.error
    import urllib.request

    def _fake_urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url="https://api.anthropic.com", code=401, msg="Unauthorized",
            hdrs=None, fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg={
        "llm": {"anthropic": {"api_key": "k"}},
    }))
    assert r.ok is True
    assert "HTTP 401" in r.detail


def test_connectivity_unreachable_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS / connect / TLS failures count as unreachable."""
    import urllib.error
    import urllib.request

    def _fake_urlopen(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg={
        "llm": {"anthropic": {"api_key": "k"}},
    }))
    assert r.ok is False
    assert "unreachable" in r.detail
    assert r.advisory is not None


def test_connectivity_honors_base_url_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user points at a proxy or self-hosted endpoint via
    ``base_url``, we probe *that* URL — not the upstream default."""
    import urllib.request

    probed_urls: list[str] = []

    class _FakeResponse:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    def _fake_urlopen(req, timeout):  # noqa: ARG001
        probed_urls.append(req.full_url)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    check = ConnectivityCheck()
    r = check.run(_connectivity_ctx(tmp_path, probe_network=True, cfg={
        "llm": {
            "anthropic": {
                "api_key": "k",
                "base_url": "https://proxy.example.com",
            },
        },
    }))
    assert r.ok is True
    assert probed_urls == ["https://proxy.example.com"]


# ── StalePidCheck ────────────────────────────────────────────────────────

def _pid_ctx(tmp_path: Path, pid_path: Path) -> DoctorContext:
    ctx = DoctorContext(config_path=tmp_path / "unused.json")
    ctx.extras["pid_path"] = pid_path
    return ctx


def test_pid_lock_no_file_is_ok(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import StalePidCheck

    check = StalePidCheck()
    r = check.run(_pid_ctx(tmp_path, tmp_path / "daemon.pid"))
    assert r.ok is True
    assert "no daemon tracked" in r.detail
    assert r.fix_available is False


def test_pid_lock_malformed_file_is_fixable(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import StalePidCheck

    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("not-an-int", encoding="utf-8")
    check = StalePidCheck()
    ctx = _pid_ctx(tmp_path, pid_path)
    r = check.run(ctx)
    assert r.ok is False
    assert r.fix_available is True

    # fix clears the file.
    assert check.fix(ctx) is True
    assert not pid_path.exists()
    assert check.run(ctx).ok is True


def test_pid_lock_alive_process_returns_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xmclaw.cli.doctor_registry import StalePidCheck

    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(
        "xmclaw.daemon.lifecycle._process_alive", lambda _pid: True,
    )
    check = StalePidCheck()
    r = check.run(_pid_ctx(tmp_path, pid_path))
    assert r.ok is True
    assert "12345" in r.detail
    assert r.fix_available is False


def test_pid_lock_stale_file_is_fixable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xmclaw.cli.doctor_registry import StalePidCheck

    pid_path = tmp_path / "daemon.pid"
    meta_path = tmp_path / "daemon.meta"
    pid_path.write_text("99999", encoding="utf-8")
    meta_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "xmclaw.daemon.lifecycle._process_alive", lambda _pid: False,
    )
    check = StalePidCheck()
    ctx = _pid_ctx(tmp_path, pid_path)
    r = check.run(ctx)
    assert r.ok is False
    assert "stale" in r.detail
    assert "99999" in r.detail
    assert r.fix_available is True

    # fix removes both pid + meta.
    assert check.fix(ctx) is True
    assert not pid_path.exists()
    assert not meta_path.exists()
    # Re-run now reports OK.
    r2 = check.run(ctx)
    assert r2.ok is True


def test_pid_lock_fix_tolerates_missing_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """meta file is optional — fix() must not fail when it's absent."""
    from xmclaw.cli.doctor_registry import StalePidCheck

    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("42", encoding="utf-8")
    monkeypatch.setattr(
        "xmclaw.daemon.lifecycle._process_alive", lambda _pid: False,
    )
    check = StalePidCheck()
    ctx = _pid_ctx(tmp_path, pid_path)
    assert check.fix(ctx) is True
    assert not pid_path.exists()


# ── ConfigCheck (auto-fix) ──────────────────────────────────────────────


def test_config_check_fixable_when_file_missing(tmp_path: Path) -> None:
    """Missing config file is the one fixable ConfigCheck failure mode."""
    from xmclaw.cli.doctor_registry import ConfigCheck

    cfg_path = tmp_path / "daemon" / "config.json"
    ctx = DoctorContext(config_path=cfg_path)
    check = ConfigCheck()
    r = check.run(ctx)
    assert r.ok is False
    assert r.fix_available is True
    assert r.advisory is not None
    assert "--fix" in r.advisory


def test_config_check_not_fixable_when_file_invalid(tmp_path: Path) -> None:
    """A user-created file with bad JSON must not be silently overwritten."""
    from xmclaw.cli.doctor_registry import ConfigCheck

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not json", encoding="utf-8")
    ctx = DoctorContext(config_path=cfg_path)
    check = ConfigCheck()
    r = check.run(ctx)
    assert r.ok is False
    assert r.fix_available is False


def test_config_check_not_fixable_when_root_is_array(tmp_path: Path) -> None:
    """Root-is-not-dict is also user data we shouldn't overwrite."""
    from xmclaw.cli.doctor_registry import ConfigCheck

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("[]", encoding="utf-8")
    ctx = DoctorContext(config_path=cfg_path)
    check = ConfigCheck()
    r = check.run(ctx)
    assert r.ok is False
    assert r.fix_available is False


def test_config_check_fix_writes_skeleton(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import ConfigCheck

    cfg_path = tmp_path / "daemon" / "config.json"
    ctx = DoctorContext(config_path=cfg_path)
    check = ConfigCheck()
    assert check.fix(ctx) is True
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    # Skeleton must be daemon-bootable.
    assert data["llm"]["default_provider"] == "anthropic"
    assert "gateway" in data
    # Re-running the check now succeeds AND populates ctx.cfg.
    r = check.run(ctx)
    assert r.ok is True
    assert ctx.cfg is not None


def test_config_check_fix_refuses_to_overwrite(tmp_path: Path) -> None:
    """fix() on an existing (even malformed) file must return False."""
    from xmclaw.cli.doctor_registry import ConfigCheck

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not json", encoding="utf-8")
    ctx = DoctorContext(config_path=cfg_path)
    check = ConfigCheck()
    assert check.fix(ctx) is False
    # The broken file must be untouched.
    assert cfg_path.read_text(encoding="utf-8") == "{not json"


def test_config_check_fix_template_matches_config_init_template() -> None:
    """ConfigCheck.fix() and ``xmclaw config init`` must use the same template
    so the two recovery paths don't drift."""
    from xmclaw.cli.config_template import default_config_template
    from xmclaw.cli.main import _default_config_template

    assert _default_config_template() == default_config_template()


# ── PairingCheck (auto-fix) ─────────────────────────────────────────────

def _pairing_ctx(tmp_path: Path, token_path: Path) -> DoctorContext:
    ctx = DoctorContext(config_path=tmp_path / "unused.json")
    ctx.token_path = token_path
    return ctx


def test_pairing_check_not_yet_created_is_ok(tmp_path: Path) -> None:
    """Missing token file is expected — serve creates it. Not fixable."""
    from xmclaw.cli.doctor_registry import PairingCheck

    check = PairingCheck()
    r = check.run(_pairing_ctx(tmp_path, tmp_path / "tok.txt"))
    assert r.ok is True
    assert r.fix_available is False


def test_pairing_check_healthy_token_is_ok(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import PairingCheck

    p = tmp_path / "tok.txt"
    p.write_text("a" * 64, encoding="utf-8")
    if sys.platform != "win32":
        import os as _os
        _os.chmod(p, 0o600)
    check = PairingCheck()
    r = check.run(_pairing_ctx(tmp_path, p))
    assert r.ok is True
    assert r.fix_available is False


def test_pairing_check_empty_file_is_fixable(tmp_path: Path) -> None:
    """Empty token file => fix by unlinking so serve regenerates."""
    from xmclaw.cli.doctor_registry import PairingCheck

    p = tmp_path / "tok.txt"
    p.write_text("", encoding="utf-8")
    check = PairingCheck()
    ctx = _pairing_ctx(tmp_path, p)
    r = check.run(ctx)
    assert r.ok is False
    assert r.fix_available is True
    assert "empty" in r.detail
    assert r.advisory is not None and "--fix" in r.advisory

    assert check.fix(ctx) is True
    assert not p.exists()
    # Post-fix the check reports the "not yet created" OK state.
    assert check.run(ctx).ok is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perm semantics")
def test_pairing_check_loose_perms_is_fixable(tmp_path: Path) -> None:
    """World-readable token => chmod 600 keeps the token, tightens perms."""
    import os as _os
    from xmclaw.cli.doctor_registry import PairingCheck

    p = tmp_path / "tok.txt"
    p.write_text("a" * 64, encoding="utf-8")
    _os.chmod(p, 0o644)
    check = PairingCheck()
    ctx = _pairing_ctx(tmp_path, p)
    r = check.run(ctx)
    assert r.ok is False
    assert r.fix_available is True
    assert "loose perms" in r.detail

    assert check.fix(ctx) is True
    # The token itself must survive the fix.
    assert p.read_text(encoding="utf-8") == "a" * 64
    mode = _os.stat(p).st_mode & 0o777
    assert mode == 0o600
    assert check.run(ctx).ok is True


def test_pairing_check_fix_noop_when_nothing_to_do(tmp_path: Path) -> None:
    """fix() on a non-existent token file returns False (nothing to repair)."""
    from xmclaw.cli.doctor_registry import PairingCheck

    check = PairingCheck()
    ctx = _pairing_ctx(tmp_path, tmp_path / "never_created.txt")
    assert check.fix(ctx) is False


# ── DoctorRegistry.run_fixes ─────────────────────────────────────────────


class _FixableFailingCheck(DoctorCheck):
    """Fails on first run, succeeds after fix() flips a flag."""

    id = "fixable_fail"
    name = "fixable_fail"

    def __init__(self) -> None:
        self._fixed = False
        self.fix_calls = 0

    def run(self, ctx: DoctorContext) -> RegistryCheckResult:
        if self._fixed:
            return RegistryCheckResult(
                name=self.name, ok=True, detail="now green",
            )
        return RegistryCheckResult(
            name=self.name, ok=False, detail="red",
            advisory="run --fix", fix_available=True,
        )

    def fix(self, ctx: DoctorContext) -> bool:
        self.fix_calls += 1
        self._fixed = True
        return True


class _FixRaisingCheck(DoctorCheck):
    id = "fix_raises"
    name = "fix_raises"

    def run(self, ctx: DoctorContext) -> RegistryCheckResult:
        return RegistryCheckResult(
            name=self.name, ok=False, detail="busted",
            fix_available=True,
        )

    def fix(self, ctx: DoctorContext) -> bool:
        raise RuntimeError("cannot recover")


def test_run_fixes_skips_passing_checks(tmp_path: Path) -> None:
    reg = DoctorRegistry()
    reg.register(_PassingCheck())
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    attempts = reg.run_fixes(ctx, results)
    assert attempts == []


def test_run_fixes_skips_checks_without_fix_available(tmp_path: Path) -> None:
    """A failing check that didn't set ``fix_available`` must be left alone."""
    reg = DoctorRegistry()
    reg.register(_FailingCheck())  # fix_available defaults to False
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    attempts = reg.run_fixes(ctx, results)
    assert attempts == []


def test_run_fixes_resolves_fixable_failure(tmp_path: Path) -> None:
    reg = DoctorRegistry()
    fixable = _FixableFailingCheck()
    reg.register(fixable)
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    assert not results[0].ok

    attempts = reg.run_fixes(ctx, results)
    assert len(attempts) == 1
    att = attempts[0]
    assert att.check_id == "fixable_fail"
    assert att.before.ok is False
    assert att.after.ok is True
    assert att.fix_raised is None
    assert fixable.fix_calls == 1


def test_run_fixes_captures_exception_from_fix(tmp_path: Path) -> None:
    reg = DoctorRegistry()
    reg.register(_FixRaisingCheck())
    ctx = DoctorContext(config_path=_write_valid_cfg(tmp_path))
    results = reg.run_all(ctx)
    attempts = reg.run_fixes(ctx, results)
    assert len(attempts) == 1
    att = attempts[0]
    assert att.fix_raised is not None
    assert "RuntimeError" in att.fix_raised
    # Still runs the re-check and reports the state (still red).
    assert att.after.ok is False


def test_run_fixes_workspace_end_to_end(tmp_path: Path) -> None:
    """Full loop: register default set (which includes WorkspaceCheck),
    point it at a missing tmp dir, run, --fix, verify green."""
    reg = build_default_registry()
    ws = tmp_path / "v2_workspace"
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    ctx.extras["workspace_dir"] = ws

    results = reg.run_all(ctx)
    ws_before = next(r for r in results if r.name == "workspace")
    assert not ws_before.ok
    assert ws_before.fix_available

    attempts = reg.run_fixes(ctx, results)
    ws_attempt = next(a for a in attempts if a.check_id == "workspace")
    assert ws_attempt.after.ok
    assert ws.is_dir()


# ── CLI --fix integration ────────────────────────────────────────────────


def test_cli_fix_creates_workspace_and_exits_zero(tmp_path: Path) -> None:
    """End-to-end: ``xmclaw doctor --fix`` with a missing workspace.

    We can't pass ctx.extras through the CLI today, so the test points
    the default WorkspaceCheck at tmp via HOME override — that's what
    ``Path.home()`` reads on every major platform.
    """
    import os
    from typer.testing import CliRunner

    from xmclaw.cli.main import app

    cfg_path = _write_valid_cfg(tmp_path)

    # Build an isolated HOME so ~/.xmclaw/v2 resolves into tmp_path.
    home = tmp_path / "home"
    home.mkdir()
    env_vars = {
        "HOME": str(home),                # POSIX
        "USERPROFILE": str(home),         # Windows
        "XMC_V2_PAIRING_TOKEN_PATH": str(tmp_path / "pair.txt"),
    }
    old = {k: os.environ.get(k) for k in env_vars}
    os.environ.update(env_vars)
    try:
        runner = CliRunner()
        # First run: workspace missing → fails.
        r_before = runner.invoke(app, [
            "doctor", "--config", str(cfg_path), "--no-daemon-probe",
            "--port", "54331", "--json",
        ])
        assert r_before.exit_code == 1, r_before.output
        body_before = json.loads(r_before.output)
        ws_row = next(c for c in body_before["checks"] if c["name"] == "workspace")
        assert not ws_row["ok"]
        assert ws_row["fix_available"] is True

        # Second run: --fix creates the dir, exit 0.
        r_after = runner.invoke(app, [
            "doctor", "--config", str(cfg_path), "--no-daemon-probe",
            "--port", "54331", "--fix", "--json",
        ])
        assert r_after.exit_code == 0, r_after.output
        body_after = json.loads(r_after.output)
        ws_row_after = next(
            c for c in body_after["checks"] if c["name"] == "workspace"
        )
        assert ws_row_after["ok"]
        assert (home / ".xmclaw" / "v2").is_dir()

        # Fix-attempts summary must reference the workspace check.
        ids = [a["check_id"] for a in body_after["fix_attempts"]]
        assert "workspace" in ids
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_cli_fix_text_output_includes_summary_block(tmp_path: Path) -> None:
    """The human-readable output gets a ``fix attempts:`` section when --fix ran."""
    import os
    from typer.testing import CliRunner

    from xmclaw.cli.main import app

    cfg_path = _write_valid_cfg(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env_vars = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XMC_V2_PAIRING_TOKEN_PATH": str(tmp_path / "pair.txt"),
    }
    old = {k: os.environ.get(k) for k in env_vars}
    os.environ.update(env_vars)
    try:
        runner = CliRunner()
        r = runner.invoke(app, [
            "doctor", "--config", str(cfg_path), "--no-daemon-probe",
            "--port", "54332", "--fix",
        ])
        assert r.exit_code == 0, r.output
        assert "fix attempts:" in r.output
        assert "workspace" in r.output
        assert "resolved" in r.output
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── MemoryDbCheck ────────────────────────────────────────────────────────


def _memory_ctx(
    tmp_path: Path, db: Path | None, *, cfg: dict | None = None,
) -> DoctorContext:
    ctx = DoctorContext(config_path=tmp_path / "unused.json")
    ctx.cfg = cfg
    if db is not None:
        ctx.extras["memory_db_path"] = db
    return ctx


def test_memory_db_disabled_is_ok_and_skips_probe(tmp_path: Path) -> None:
    """memory.enabled=false means no daemon store — nothing to probe."""
    from xmclaw.cli.doctor_registry import MemoryDbCheck

    check = MemoryDbCheck()
    ctx = DoctorContext(config_path=tmp_path / "unused.json")
    ctx.cfg = {"memory": {"enabled": False}}
    # Deliberately no override — disabled short-circuits before path resolution.
    r = check.run(ctx)
    assert r.ok is True
    assert "disabled" in r.detail


def test_memory_db_missing_file_is_ok(tmp_path: Path) -> None:
    """No memory.db yet — SqliteVecMemory creates it lazily, not a failure."""
    from xmclaw.cli.doctor_registry import MemoryDbCheck

    check = MemoryDbCheck()
    r = check.run(_memory_ctx(tmp_path, tmp_path / "memory.db"))
    assert r.ok is True
    assert "not yet created" in r.detail


def test_memory_db_path_is_a_directory_fails(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import MemoryDbCheck

    db = tmp_path / "memory.db"
    db.mkdir()
    check = MemoryDbCheck()
    r = check.run(_memory_ctx(tmp_path, db))
    assert r.ok is False
    assert "not a file" in r.detail


def test_memory_db_garbage_file_reports_parse_error(tmp_path: Path) -> None:
    """A non-SQLite file at the db path must fail parse."""
    from xmclaw.cli.doctor_registry import MemoryDbCheck

    db = tmp_path / "memory.db"
    db.write_bytes(b"not a sqlite db")
    check = MemoryDbCheck()
    r = check.run(_memory_ctx(tmp_path, db))
    assert r.ok is False
    assert "malformed" in r.detail or "cannot open" in r.detail


def test_memory_db_sqlite_without_memory_items_table_fails(tmp_path: Path) -> None:
    """A foreign SQLite file at memory.db — don't mis-diagnose as healthy."""
    import sqlite3

    from xmclaw.cli.doctor_registry import MemoryDbCheck

    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other_table (x INTEGER)")
    conn.close()
    check = MemoryDbCheck()
    r = check.run(_memory_ctx(tmp_path, db))
    assert r.ok is False
    assert "no memory_items table" in r.detail


def test_memory_db_healthy_shows_item_count(tmp_path: Path) -> None:
    """A real SqliteVecMemory file should report healthy + item count."""
    import sqlite3

    from xmclaw.cli.doctor_registry import MemoryDbCheck

    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, layer TEXT, text TEXT,
            metadata TEXT, ts REAL, has_embedding INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO memory_items VALUES ('a', 'short', 'x', '{}', 0.0, 0)"
    )
    conn.commit()
    conn.close()
    check = MemoryDbCheck()
    r = check.run(_memory_ctx(tmp_path, db))
    assert r.ok is True
    assert "healthy" in r.detail
    assert "1 item" in r.detail


def test_memory_db_honors_cfg_db_path(tmp_path: Path) -> None:
    """``cfg.memory.db_path`` should be probed when no extras override."""
    from xmclaw.cli.doctor_registry import MemoryDbCheck

    db = tmp_path / "custom.db"
    cfg = {"memory": {"enabled": True, "db_path": str(db)}}
    check = MemoryDbCheck()
    # No extras override — fall through to cfg.memory.db_path.
    r = check.run(_memory_ctx(tmp_path, None, cfg=cfg))
    assert r.ok is True
    assert "not yet created" in r.detail
    assert str(db) in r.detail


# ── SkillRuntimeCheck ───────────────────────────────────────────────────


def _runtime_ctx(tmp_path: Path, cfg: dict | None) -> DoctorContext:
    ctx = DoctorContext(config_path=tmp_path / "unused.json")
    ctx.cfg = cfg
    return ctx


def test_skill_runtime_no_cfg_skips(tmp_path: Path) -> None:
    """ConfigCheck failed — don't double-report."""
    from xmclaw.cli.doctor_registry import SkillRuntimeCheck

    check = SkillRuntimeCheck()
    r = check.run(_runtime_ctx(tmp_path, None))
    assert r.ok is True
    assert "skipped" in r.detail


def test_skill_runtime_section_absent_defaults_local(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import SkillRuntimeCheck

    check = SkillRuntimeCheck()
    r = check.run(_runtime_ctx(tmp_path, {}))
    assert r.ok is True
    assert "local" in r.detail


def test_skill_runtime_local_backend_is_ok(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import SkillRuntimeCheck

    check = SkillRuntimeCheck()
    r = check.run(_runtime_ctx(tmp_path, {"runtime": {"backend": "local"}}))
    assert r.ok is True
    assert "local" in r.detail
    assert "LocalSkillRuntime" in r.detail


def test_skill_runtime_process_backend_is_ok(tmp_path: Path) -> None:
    from xmclaw.cli.doctor_registry import SkillRuntimeCheck

    check = SkillRuntimeCheck()
    r = check.run(_runtime_ctx(tmp_path, {"runtime": {"backend": "process"}}))
    assert r.ok is True
    assert "process" in r.detail
    assert "ProcessSkillRuntime" in r.detail


def test_skill_runtime_unknown_backend_fails_with_known_set(tmp_path: Path) -> None:
    """A typo should surface with the known-backend list so the user can fix it."""
    from xmclaw.cli.doctor_registry import SkillRuntimeCheck

    check = SkillRuntimeCheck()
    r = check.run(_runtime_ctx(tmp_path, {"runtime": {"backend": "docker"}}))
    assert r.ok is False
    assert "docker" in r.detail
    assert "local" in r.detail and "process" in r.detail
    assert r.advisory is not None


def test_skill_runtime_non_dict_section_fails(tmp_path: Path) -> None:
    """A scalar at cfg['runtime'] is a clear config bug — surface it."""
    from xmclaw.cli.doctor_registry import SkillRuntimeCheck

    check = SkillRuntimeCheck()
    r = check.run(_runtime_ctx(tmp_path, {"runtime": "local"}))
    assert r.ok is False
    assert "object" in r.detail or "dict" in r.detail.lower()


# ── Epic #10 + #20: BackupsCheck ────────────────────────────────────────


def _backups_ctx(tmp_path: Path, backups_dir: Path) -> DoctorContext:
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    ctx.extras["backups_dir"] = backups_dir
    return ctx


def _seed_backup(backups_dir: Path, name: str, *, age_seconds: float) -> None:
    """Plant a minimally-well-formed backup dir with a manifest dated
    ``age_seconds`` in the past. The archive is a single-byte placeholder
    since BackupsCheck doesn't touch it."""
    import time as _time

    from xmclaw.backup.manifest import (
        MANIFEST_NAME,
        MANIFEST_SCHEMA_VERSION,
        Manifest,
    )
    from xmclaw.backup.store import ARCHIVE_NAME

    bdir = backups_dir / name
    bdir.mkdir(parents=True)
    (bdir / ARCHIVE_NAME).write_bytes(b"x")
    Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        name=name,
        created_ts=_time.time() - age_seconds,
        xmclaw_version="0.0.0-test",
        archive_sha256="0" * 64,
        archive_bytes=1,
        source_dir=str(backups_dir),
        excluded=(),
        entries=0,
    ).write(bdir / MANIFEST_NAME)


def test_backups_empty_is_ok_with_create_hint(tmp_path: Path) -> None:
    """No backups = ok + advisory nudging toward `xmclaw backup create`."""
    from xmclaw.cli.doctor_registry import BackupsCheck

    check = BackupsCheck()
    r = check.run(_backups_ctx(tmp_path, tmp_path / "nobackups"))
    assert r.ok is True
    assert "no backups" in r.detail
    assert r.advisory is not None
    assert "xmclaw backup create" in r.advisory


def test_backups_fresh_is_ok_without_advisory(tmp_path: Path) -> None:
    """A single fresh backup is a happy steady-state — no nag."""
    from xmclaw.cli.doctor_registry import BackupsCheck

    bdir = tmp_path / "backups"
    bdir.mkdir()
    _seed_backup(bdir, "recent", age_seconds=3600)  # 1h old
    r = BackupsCheck().run(_backups_ctx(tmp_path, bdir))
    assert r.ok is True
    assert "1 backup(s)" in r.detail
    assert "recent" in r.detail
    assert r.advisory is None


def test_backups_stale_advises_refresh(tmp_path: Path) -> None:
    """The newest backup being >30 days old triggers the stale advisory."""
    from xmclaw.cli.doctor_registry import BackupsCheck

    bdir = tmp_path / "backups"
    bdir.mkdir()
    _seed_backup(bdir, "old", age_seconds=45 * 86400)  # 45d old
    r = BackupsCheck().run(_backups_ctx(tmp_path, bdir))
    assert r.ok is True  # still informational, not a failure
    assert "1 backup(s)" in r.detail
    assert r.advisory is not None
    assert "old" in r.advisory or "d old" in r.advisory
    assert "xmclaw backup create" in r.advisory


def test_backups_reports_newest_when_multiple(tmp_path: Path) -> None:
    """Detail must name the newest backup and the correct count."""
    from xmclaw.cli.doctor_registry import BackupsCheck

    bdir = tmp_path / "backups"
    bdir.mkdir()
    _seed_backup(bdir, "old", age_seconds=10 * 86400)
    _seed_backup(bdir, "newer", age_seconds=1 * 86400)
    _seed_backup(bdir, "newest", age_seconds=3600)
    r = BackupsCheck().run(_backups_ctx(tmp_path, bdir))
    assert r.ok is True
    assert "3 backup(s)" in r.detail
    assert "newest" in r.detail  # picks the actually-newest
    # When the newest is fresh, no advisory regardless of older siblings.
    assert r.advisory is None


def test_backups_honors_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ctx override, BackupsCheck falls through to XMC_BACKUPS_DIR."""
    from xmclaw.cli.doctor_registry import BackupsCheck

    bdir = tmp_path / "envbackups"
    bdir.mkdir()
    _seed_backup(bdir, "env_one", age_seconds=60)
    monkeypatch.setenv("XMC_BACKUPS_DIR", str(bdir))
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    # no ctx.extras["backups_dir"] — must fall through to env.
    r = BackupsCheck().run(ctx)
    assert r.ok is True
    assert "env_one" in r.detail


# ── Epic #10 + #16: SecretsCheck ────────────────────────────────────────


def _secrets_ctx(
    tmp_path: Path, secrets_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> DoctorContext:
    """DoctorContext wired to a tmp-path secrets.json.

    Also pins ``XMC_SECRETS_PATH`` because
    :func:`xmclaw.utils.secrets.secrets_file_path` is what ``list_secret_names``
    / ``iter_env_override_names`` consult — those helpers don't see
    ``ctx.extras``. Clears host-leaked ``XMC_SECRET_*`` for determinism.
    """
    monkeypatch.setenv("XMC_SECRETS_PATH", str(secrets_path))
    for k in list(os.environ):
        if k.startswith("XMC_SECRET_"):
            monkeypatch.delenv(k, raising=False)
    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    ctx.extras["secrets_path"] = secrets_path
    return ctx


def test_secrets_missing_file_is_ok_with_create_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No secrets file = ok + advisory pointing at set-secret CLI."""
    from xmclaw.cli.doctor_registry import SecretsCheck

    r = SecretsCheck().run(_secrets_ctx(
        tmp_path, tmp_path / "missing.json", monkeypatch,
    ))
    assert r.ok is True
    assert "no secrets file" in r.detail
    assert r.advisory is not None
    assert "set-secret" in r.advisory


def test_secrets_empty_file_is_ok_with_create_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file with an empty dict produces an advisory, not a failure."""
    from xmclaw.cli.doctor_registry import SecretsCheck

    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    r = SecretsCheck().run(_secrets_ctx(tmp_path, path, monkeypatch))
    assert r.ok is True
    assert "empty" in r.detail
    assert r.advisory is not None


def test_secrets_populated_file_reports_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A populated file without env overrides = ok + silent detail."""
    from xmclaw.cli.doctor_registry import SecretsCheck
    from xmclaw.utils import secrets as secrets_mod

    path = tmp_path / "secrets.json"
    monkeypatch.setenv("XMC_SECRETS_PATH", str(path))
    secrets_mod.set_secret("alpha", "a")
    secrets_mod.set_secret("beta", "b")

    ctx = _secrets_ctx(tmp_path, path, monkeypatch)
    r = SecretsCheck().run(ctx)
    assert r.ok is True
    assert "2 secret(s)" in r.detail
    assert r.advisory is None


def test_secrets_env_override_surfaces_as_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an env var shadows a file entry, advise the user."""
    from xmclaw.cli.doctor_registry import SecretsCheck
    from xmclaw.utils import secrets as secrets_mod

    path = tmp_path / "secrets.json"
    monkeypatch.setenv("XMC_SECRETS_PATH", str(path))
    secrets_mod.set_secret("shadowed", "v")
    secrets_mod.set_secret("not_shadowed", "w")
    monkeypatch.setenv("XMC_SECRET_SHADOWED", "from-env")

    ctx = _secrets_ctx(tmp_path, path, monkeypatch)
    # re-apply XMC_SECRET_SHADOWED since _secrets_ctx clears XMC_SECRET_*
    monkeypatch.setenv("XMC_SECRET_SHADOWED", "from-env")
    r = SecretsCheck().run(ctx)
    assert r.ok is True
    assert r.advisory is not None
    assert "shadowed" in r.advisory
    assert "1 entry" in r.advisory or "1 " in r.advisory


def test_secrets_many_overrides_truncates_advisory_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With >3 overrides the advisory lists the first 3 + ``+N more``."""
    from xmclaw.cli.doctor_registry import SecretsCheck
    from xmclaw.utils import secrets as secrets_mod

    path = tmp_path / "secrets.json"
    monkeypatch.setenv("XMC_SECRETS_PATH", str(path))
    for i in range(5):
        secrets_mod.set_secret(f"k{i}", "v")
    for i in range(5):
        monkeypatch.setenv(f"XMC_SECRET_K{i}", "env")

    ctx = _secrets_ctx(tmp_path, path, monkeypatch)
    for i in range(5):
        monkeypatch.setenv(f"XMC_SECRET_K{i}", "env")
    r = SecretsCheck().run(ctx)
    assert r.ok is True
    assert r.advisory is not None
    assert "+2 more" in r.advisory


def test_secrets_loose_mode_fails_with_fix_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX-only: a world-readable secrets.json fails loud + fix_available."""
    if os.name != "posix":
        pytest.skip("file-mode semantics are POSIX-only")
    from xmclaw.cli.doctor_registry import SecretsCheck

    path = tmp_path / "secrets.json"
    path.write_text('{"k": "v"}', encoding="utf-8")
    os.chmod(path, 0o644)

    ctx = _secrets_ctx(tmp_path, path, monkeypatch)
    r = SecretsCheck().run(ctx)
    assert r.ok is False
    assert "0o644" in r.detail or "readable" in r.detail.lower()
    assert r.fix_available is True


def test_secrets_check_fix_tightens_mode_to_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SecretsCheck.fix() chmods a loose file back to 0600 on POSIX."""
    if os.name != "posix":
        pytest.skip("chmod semantics are POSIX-only")
    from xmclaw.cli.doctor_registry import SecretsCheck

    path = tmp_path / "secrets.json"
    path.write_text('{"k": "v"}', encoding="utf-8")
    os.chmod(path, 0o640)

    check = SecretsCheck()
    ctx = _secrets_ctx(tmp_path, path, monkeypatch)
    assert check.fix(ctx) is True
    assert (path.stat().st_mode & 0o777) == 0o600
    # Re-run: now healthy.
    r = check.run(ctx)
    assert r.ok is True


def test_secrets_check_fix_noop_when_already_tight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fix() returns False when there's nothing to tighten — keeps the
    Epic #10 auto-fix counter honest (no 'fixed' spam for pristine files)."""
    if os.name != "posix":
        pytest.skip("chmod semantics are POSIX-only")
    from xmclaw.cli.doctor_registry import SecretsCheck

    path = tmp_path / "secrets.json"
    path.write_text('{"k": "v"}', encoding="utf-8")
    os.chmod(path, 0o600)

    check = SecretsCheck()
    ctx = _secrets_ctx(tmp_path, path, monkeypatch)
    assert check.fix(ctx) is False


def test_secrets_check_fix_missing_file_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fix() returns False (not a crash) when the file simply doesn't exist."""
    from xmclaw.cli.doctor_registry import SecretsCheck

    check = SecretsCheck()
    ctx = _secrets_ctx(tmp_path, tmp_path / "absent.json", monkeypatch)
    assert check.fix(ctx) is False


def test_secrets_check_honors_file_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ctx.extras, SecretsCheck falls through to secrets_file_path()."""
    from xmclaw.cli.doctor_registry import SecretsCheck

    path = tmp_path / "fallthrough.json"
    monkeypatch.setenv("XMC_SECRETS_PATH", str(path))
    for k in list(os.environ):
        if k.startswith("XMC_SECRET_"):
            monkeypatch.delenv(k, raising=False)

    ctx = DoctorContext(
        config_path=_write_valid_cfg(tmp_path),
        probe_daemon=False,
    )
    # deliberately no ctx.extras["secrets_path"] — should fall through.
    r = SecretsCheck().run(ctx)
    assert r.ok is True
    assert "no secrets file" in r.detail
    assert str(path) in r.detail
