"""Central runtime-path resolution — §3.1 contract.

Every runtime path must flow through ``xmclaw.utils.paths``. This suite
pins:

* ``XMC_DATA_DIR`` reroutes the entire workspace (the §3.1 Docker /
  portable-install lever).
* Narrow legacy overrides (``XMC_V2_PID_PATH``, ``XMC_V2_PAIRING_TOKEN_PATH``,
  ``XMC_V2_EVENTS_DB_PATH``) still win for their specific files — tests
  use them to isolate a single fixture without moving everything.
* Existing ``default_*_path`` entry points in daemon/pairing/lifecycle/bus
  delegate to the central module, so a future ``--data-dir`` CLI flag can
  push through one spot instead of chasing scattered ``Path.home()`` calls.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_env_bleed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the env vars this suite exercises so test order can't bleed."""
    for var in (
        "XMC_DATA_DIR",
        "XMC_V2_PID_PATH",
        "XMC_V2_PAIRING_TOKEN_PATH",
        "XMC_V2_EVENTS_DB_PATH",
    ):
        monkeypatch.delenv(var, raising=False)


# ── XMC_DATA_DIR moves the whole workspace ────────────────────────────────

def test_data_dir_defaults_to_home_xmclaw(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from xmclaw.utils import paths
    assert paths.data_dir() == tmp_path / ".xmclaw"


def test_data_dir_honors_xmc_data_dir(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "portable_install"
    monkeypatch.setenv("XMC_DATA_DIR", str(target))
    from xmclaw.utils import paths
    assert paths.data_dir() == target


def test_v2_workspace_dir_is_child_of_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.utils import paths
    assert paths.v2_workspace_dir() == tmp_path / "v2"


def test_logs_dir_is_peer_of_v2(monkeypatch, tmp_path: Path) -> None:
    """Logs are a peer of v2/ on purpose — survives a v2 wipe for post-mortem."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.utils import paths
    assert paths.logs_dir() == tmp_path / "logs"


# ── Each default_*_path() routes through the workspace ────────────────────

@pytest.mark.parametrize("fn_name, filename", [
    ("default_pid_path",        "daemon.pid"),
    ("default_meta_path",       "daemon.meta"),
    ("default_daemon_log_path", "daemon.log"),
    ("default_token_path",      "pairing_token.txt"),
    ("default_events_db_path",  "events.db"),
    ("default_memory_db_path",  "memory.db"),
])
def test_default_paths_live_in_v2_workspace(
    monkeypatch, tmp_path: Path, fn_name: str, filename: str,
) -> None:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.utils import paths
    fn = getattr(paths, fn_name)
    assert fn() == tmp_path / "v2" / filename


# ── Narrow overrides still win where they used to ─────────────────────────

def test_pid_path_honors_narrow_override(monkeypatch, tmp_path: Path) -> None:
    """The lifecycle test fixtures use ``XMC_V2_PID_PATH`` — must still work."""
    specific = tmp_path / "custom_pid.pid"
    monkeypatch.setenv("XMC_V2_PID_PATH", str(specific))
    # And XMC_DATA_DIR pointed somewhere else — narrow override wins.
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "unused_root"))
    from xmclaw.utils import paths
    assert paths.default_pid_path() == specific


def test_token_path_honors_narrow_override(monkeypatch, tmp_path: Path) -> None:
    specific = tmp_path / "custom_token.txt"
    monkeypatch.setenv("XMC_V2_PAIRING_TOKEN_PATH", str(specific))
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "unused_root"))
    from xmclaw.utils import paths
    assert paths.default_token_path() == specific


def test_events_db_path_honors_narrow_override(
    monkeypatch, tmp_path: Path,
) -> None:
    specific = tmp_path / "custom_events.db"
    monkeypatch.setenv("XMC_V2_EVENTS_DB_PATH", str(specific))
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "unused_root"))
    from xmclaw.utils import paths
    assert paths.default_events_db_path() == specific


# ── Legacy callers must delegate, not drift ───────────────────────────────

def test_pairing_default_token_path_delegates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.daemon import pairing
    from xmclaw.utils import paths
    assert pairing.default_token_path() == paths.default_token_path()
    assert pairing.default_token_path() == tmp_path / "v2" / "pairing_token.txt"


def test_lifecycle_default_pid_path_delegates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.daemon import lifecycle
    from xmclaw.utils import paths
    assert lifecycle.default_pid_path() == paths.default_pid_path()


def test_lifecycle_log_path_follows_pid_narrow_override(
    monkeypatch, tmp_path: Path,
) -> None:
    """Contract: overriding XMC_V2_PID_PATH cascades meta + log into the
    *same directory* as the pid file.

    Tests (test_v2_daemon_lifecycle) rely on this — they set only the
    PID env var and expect meta/log to land in the same tmp dir (the
    filenames stay ``daemon.meta`` / ``daemon.log`` — it's directory
    redirection that matters for test isolation).
    """
    pid = tmp_path / "daemon.pid"
    monkeypatch.setenv("XMC_V2_PID_PATH", str(pid))
    from xmclaw.daemon import lifecycle
    assert lifecycle.default_pid_path() == pid
    assert lifecycle.default_meta_path() == tmp_path / "daemon.meta"
    assert lifecycle.default_log_path() == tmp_path / "daemon.log"


def test_sqlite_default_events_db_path_delegates(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.core.bus.sqlite import default_events_db_path
    from xmclaw.utils import paths
    assert default_events_db_path() == paths.default_events_db_path()
    assert default_events_db_path() == tmp_path / "v2" / "events.db"


# ── Legacy get_logs_dir() now honors XMC_DATA_DIR (docstring fix) ─────────

def test_get_logs_dir_now_returns_data_dir_logs(
    monkeypatch, tmp_path: Path,
) -> None:
    """Pre-refactor bug: get_logs_dir() returned ``<repo>/logs`` (§3.1
    violation). log.py's docstring already promised ``~/.xmclaw/logs/``;
    this test pins the corrected behavior."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    from xmclaw.utils import paths
    assert paths.get_logs_dir() == tmp_path / "logs"
    # And the new canonical name resolves to the same place.
    assert paths.logs_dir() == tmp_path / "logs"
