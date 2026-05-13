"""Wave 23 — hidden_subprocess_kwargs() unit tests."""
from __future__ import annotations

import subprocess
import sys

import pytest

from xmclaw.utils.subprocess_hidden import hidden_subprocess_kwargs


def test_posix_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Windows, no console pop-up problem → no kwargs to inject."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert hidden_subprocess_kwargs() == {}


def test_macos_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert hidden_subprocess_kwargs() == {}


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="STARTUPINFO is Windows-only",
)
def test_windows_includes_create_no_window() -> None:
    kw = hidden_subprocess_kwargs()
    assert "creationflags" in kw
    assert "startupinfo" in kw
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert (kw["creationflags"] & expected) == expected


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="STARTUPINFO is Windows-only",
)
def test_windows_startupinfo_hides_window() -> None:
    kw = hidden_subprocess_kwargs()
    si = kw["startupinfo"]
    assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert si.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only behavior",
)
def test_real_subprocess_with_hidden_kwargs_works() -> None:
    """End-to-end: pass the kwargs into subprocess.run and verify the
    process actually executes (the test runner can't observe whether
    a console flashed, but we at least verify the kwargs don't break
    the call)."""
    kw = hidden_subprocess_kwargs()
    p = subprocess.run(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True, timeout=10,
        **kw,
    )
    assert p.returncode == 0
    assert b"ok" in p.stdout
