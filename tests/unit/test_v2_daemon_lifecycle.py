"""Unit tests for xmclaw.daemon.lifecycle -- PID-file state machine.

We don't spawn a real daemon here (that's integration territory); instead
we exercise the status / stop paths with a small long-lived subprocess
that stands in for the daemon. That keeps the test hermetic and fast
while still catching the real bugs -- stale PID files, wrong signals,
missing cleanup, etc.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from xmclaw.daemon import lifecycle


@pytest.fixture
def tmp_pid_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the PID/meta/log paths into tmp_path for isolation."""
    monkeypatch.setenv("XMC_V2_PID_PATH", str(tmp_path / "daemon.pid"))
    return tmp_path


def _spawn_sleeper() -> subprocess.Popen:
    """A throwaway long-lived process to stand in for the daemon."""
    code = "import time; time.sleep(60)"
    kwargs: dict = {
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-c", code], **kwargs)


# ── status state machine ─────────────────────────────────────────────────


def test_status_dead_when_no_pid_file(tmp_pid_env: Path) -> None:
    s = lifecycle.read_status()
    assert s.state == "dead"
    assert s.pid is None


def test_status_stale_when_pid_points_to_nothing(tmp_pid_env: Path) -> None:
    # Write a PID that's extremely unlikely to be live.
    (tmp_pid_env / "daemon.pid").write_text("9999999", encoding="utf-8")
    s = lifecycle.read_status()
    assert s.state == "stale"
    assert s.pid == 9999999


def test_status_running_when_pid_is_alive(tmp_pid_env: Path) -> None:
    proc = _spawn_sleeper()
    try:
        (tmp_pid_env / "daemon.pid").write_text(str(proc.pid), encoding="utf-8")
        (tmp_pid_env / "daemon.meta").write_text(
            '{"host": "127.0.0.1", "port": 1}', encoding="utf-8",
        )
        s = lifecycle.read_status()
        assert s.state == "running"
        assert s.pid == proc.pid
        assert s.host == "127.0.0.1"
        # healthy is False because nothing is actually listening on :1.
        assert s.healthy is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ── stop path ────────────────────────────────────────────────────────────


def test_stop_dead_is_noop(tmp_pid_env: Path) -> None:
    # No PID file -> nothing to kill; must not raise.
    s = lifecycle.stop_daemon(grace_seconds=0.5)
    assert s.state == "dead"


def test_stop_kills_process_and_clears_pid(tmp_pid_env: Path) -> None:
    proc = _spawn_sleeper()
    try:
        (tmp_pid_env / "daemon.pid").write_text(str(proc.pid), encoding="utf-8")
        (tmp_pid_env / "daemon.meta").write_text(
            '{"host": "127.0.0.1", "port": 1}', encoding="utf-8",
        )
        s = lifecycle.stop_daemon(grace_seconds=3.0)
        assert s.state == "dead"
        assert not (tmp_pid_env / "daemon.pid").exists()
        assert not (tmp_pid_env / "daemon.meta").exists()
        # Reap the child before asserting the pid is gone.
        #
        # On Linux, `os.kill(pid, 0)` on a zombie process (exited but
        # not yet waited on by the parent) does NOT raise
        # ProcessLookupError — the pid is still in /proc — so
        # `_process_alive` keeps returning True for a few ms after
        # stop_daemon has SIGTERM'd the child. That made this assert
        # flake on Ubuntu CI. `proc.wait()` reaps the zombie, after
        # which the pid truly leaves the table.
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass  # fall through — the assert below will surface it
        assert not lifecycle._process_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_cleans_stale_pid_without_signalling(tmp_pid_env: Path) -> None:
    # Stale PID -- stop should clean files without error.
    (tmp_pid_env / "daemon.pid").write_text("9999999", encoding="utf-8")
    s = lifecycle.stop_daemon(grace_seconds=0.5)
    assert s.state == "dead"
    assert not (tmp_pid_env / "daemon.pid").exists()


# ── start guard ──────────────────────────────────────────────────────────


def test_start_refuses_when_already_running(
    tmp_pid_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If status reports running, start_daemon must raise rather than
    spawn a second daemon (double-bind disaster)."""
    proc = _spawn_sleeper()
    try:
        (tmp_pid_env / "daemon.pid").write_text(str(proc.pid), encoding="utf-8")
        (tmp_pid_env / "daemon.meta").write_text(
            '{"host": "127.0.0.1", "port": 8999}', encoding="utf-8",
        )
        # Short-circuit the health check so status reports "running" --
        # we don't want to actually bind a port in a unit test.
        monkeypatch.setattr(lifecycle, "_http_healthy", lambda *a, **k: True)

        with pytest.raises(RuntimeError, match="already running"):
            lifecycle.start_daemon(
                host="127.0.0.1", port=8999,
                config="nonexistent.json", wait_seconds=0.5,
            )
    finally:
        proc.kill()
        proc.wait(timeout=5)
