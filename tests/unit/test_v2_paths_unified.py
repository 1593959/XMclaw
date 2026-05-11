"""Patch A regression test — unified-paths anti-req closure (2026-05-10).

User feedback (MEMORY.md, 2026-05-01):
    "任何用户态产物 (skill / journal / user profile) 的写路径 == 读路径，
     禁止「安装在 /a 运行读 /b」的割裂"

Pre-Patch-A 11 sites in xmclaw/ hand-built ``Path.home() / ".xmclaw" /
...`` strings, ignoring ``XMC_DATA_DIR``. This test pins the contract
from BOTH ENDS:

  1. **Every public ``paths.default_* / *_dir`` function honors
     ``XMC_DATA_DIR``** — set the env var to a tmp dir, ALL helpers
     must resolve under it.
  2. **No ``Path.home() / ".xmclaw"`` literal lives outside paths.py
     itself** — a CI-friendly ast/grep guard makes "you wrote a
     hardcoded path" loud at PR time, not at user-bug time.

These together close the unified-paths anti-req — the literal
``XMC_DATA_DIR=/x`` knob actually relocates the entire install,
matching the user's mental model.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from xmclaw.utils import paths


# Helpers we expect to honor XMC_DATA_DIR (or a narrow override env var).
# Every function listed here must, when ``XMC_DATA_DIR`` is set to a
# tmp_path, resolve its result under tmp_path (or its sibling
# ``~/.xmclaw.secret`` for secret_dir specifically — that's the one
# documented exception).
_HELPERS = [
    "data_dir",
    "v2_workspace_dir",
    "logs_dir",
    "skills_dir",
    "user_skills_dir",
    "persona_dir",
    "workspaces_dir",
    "agents_registry_dir",
    "evolution_dir",
    "journal_dir",
    "file_memory_dir",
    "default_pid_path",
    "default_meta_path",
    "default_daemon_log_path",
    "default_token_path",
    "default_events_db_path",
    "default_memory_db_path",
    "default_sessions_db_path",
    # Patch A additions:
    "default_cognitive_state_path",
    "default_graph_db_path",
    "default_experiments_db_path",
    "default_ticks_db_path",
    "evolution_proposals_dir",
    "default_decisions_db_path",
    "default_suggestions_db_path",
]


# Subdirs / SQLite files that have their own narrow-override env var.
# When the narrow var is set it should win over XMC_DATA_DIR (so
# pytest can pin one path in isolation).
_NARROW_OVERRIDES = {
    "default_pid_path": "XMC_V2_PID_PATH",
    "default_token_path": "XMC_V2_PAIRING_TOKEN_PATH",
    "default_events_db_path": "XMC_V2_EVENTS_DB_PATH",
    "default_sessions_db_path": "XMC_V2_SESSIONS_DB_PATH",
    "skills_dir": "XMC_V2_SKILLS_DIR",
    "user_skills_dir": "XMC_V2_USER_SKILLS_DIR",
    "default_cognitive_state_path": "XMC_V2_COGNITIVE_STATE_PATH",
    "default_graph_db_path": "XMC_V2_GRAPH_DB_PATH",
    "default_experiments_db_path": "XMC_V2_EXPERIMENTS_DB_PATH",
    "default_ticks_db_path": "XMC_V2_TICKS_DB_PATH",
    "default_decisions_db_path": "XMC_V2_DECISIONS_DB_PATH",
    "default_suggestions_db_path": "XMC_V2_SUGGESTIONS_DB_PATH",
}


# secret_dir is intentionally OUTSIDE data_dir (Epic #16 — keeps
# crypto root separate from workspace data so backups don't scoop
# encrypted secrets). Its own env knob is XMC_SECRET_DIR.


def test_xmc_data_dir_reroutes_every_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set XMC_DATA_DIR=tmp_path and confirm EVERY public helper
    listed in _HELPERS resolves under tmp_path."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    # Clear narrow overrides so we test the XMC_DATA_DIR fallback.
    for env_var in set(_NARROW_OVERRIDES.values()):
        monkeypatch.delenv(env_var, raising=False)

    for fn_name in _HELPERS:
        fn = getattr(paths, fn_name)
        # Helpers that take args (eval_cache_dir(suite)) are passed
        # a sample value below; the rest are zero-arg.
        if fn_name == "eval_cache_dir":
            continue
        result = Path(fn())
        # Either path itself or its parents must include tmp_path.
        assert tmp_path in result.parents or result == tmp_path, (
            f"{fn_name}() returned {result!r} which is NOT under "
            f"XMC_DATA_DIR={tmp_path} — unified-paths broken."
        )


def test_eval_cache_dir_reroutes_with_xmc_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eval_cache_dir takes a suite arg; check both no-arg and
    per-suite paths reroute."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    parent = paths.eval_cache_dir()
    assert tmp_path in parent.parents

    for suite in ("longmemeval", "swe_bench_verified", "terminal_bench"):
        sub = paths.eval_cache_dir(suite)
        assert tmp_path in sub.parents
        assert sub.name == suite
        assert sub.parent == parent


def test_narrow_override_wins_over_xmc_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each helper that documents a narrow override must respect it
    even when XMC_DATA_DIR is also set. Pytest fixtures rely on this
    to pin one file without moving the whole workspace."""
    other = tmp_path / "data"
    monkeypatch.setenv("XMC_DATA_DIR", str(other))
    for fn_name, env_var in _NARROW_OVERRIDES.items():
        narrow_target = tmp_path / f"narrow_{fn_name}"
        monkeypatch.setenv(env_var, str(narrow_target))
        fn = getattr(paths, fn_name)
        assert Path(fn()) == narrow_target, (
            f"{fn_name}: narrow {env_var}={narrow_target} ignored "
            f"(got {fn()})"
        )
        monkeypatch.delenv(env_var)


def test_no_hardcoded_xmclaw_path_outside_paths_module() -> None:
    """CI guard: no .py file under xmclaw/ should hand-build
    ``Path.home() / ".xmclaw"``. Only paths.py itself (the canonical
    home-dir resolver) is allowed.

    This test is what closes the loop — once pat A flips all 11
    callsites, this guard PREVENTS the next contributor from re-
    introducing the bug. Same posture as
    ``scripts/check_import_direction.py``: lint-as-test.
    """
    repo_root = Path(__file__).resolve().parents[2]
    xmclaw_root = repo_root / "xmclaw"
    # Allowed file: paths.py itself.
    allowed = {xmclaw_root / "utils" / "paths.py"}
    # Pattern: ``Path.home()`` followed (within ~30 chars to skip
    # whitespace + slashes) by ``".xmclaw"`` literal. This catches
    # ``Path.home() / ".xmclaw"`` and minor variations.
    pattern = re.compile(
        r"Path\.home\(\)[\s\/]*[\"']\.xmclaw",
    )
    offenders: list[str] = []
    for py in xmclaw_root.rglob("*.py"):
        if py in allowed:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern.search(text):
            # Find the line for a useful error message.
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(
                        f"{py.relative_to(repo_root)}:{i} {line.strip()}",
                    )
    assert not offenders, (
        "Patch A regression — these files hand-build ``Path.home() / "
        "\".xmclaw\"`` instead of using xmclaw.utils.paths.* helpers:\n"
        + "\n".join(f"  * {o}" for o in offenders)
        + "\n\nRoute through paths.py so XMC_DATA_DIR overrides actually "
          "reroute the install."
    )


def test_no_hardcoded_xmclaw_path_in_app_lifespan_or_factories() -> None:
    """Belt-and-braces: explicitly check the daemon's hot paths
    (factory + app + lifespan) since those were the worst Patch-A
    offenders."""
    repo_root = Path(__file__).resolve().parents[2]
    hot_paths = [
        repo_root / "xmclaw" / "daemon" / "app.py",
        repo_root / "xmclaw" / "daemon" / "factory.py",
    ]
    pattern = re.compile(r"Path\.home\(\)[\s\/]*[\"']\.xmclaw")
    for hp in hot_paths:
        if not hp.exists():
            continue
        text = hp.read_text(encoding="utf-8")
        assert not pattern.search(text), (
            f"{hp} still hand-builds ``Path.home() / \".xmclaw\"`` "
            "— must route through xmclaw.utils.paths.*"
        )


def test_paths_helpers_return_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: every helper returns a Path object (not str / None)."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    for fn_name in _HELPERS:
        if fn_name == "eval_cache_dir":
            continue
        fn = getattr(paths, fn_name)
        result = fn()
        assert isinstance(result, Path), (
            f"{fn_name}() returned {type(result).__name__}, expected Path"
        )
