"""Static-scaffold guards for ``lib/use_safe_fetch.js`` (audit B6).

Pure-JS hooks can't be unit-tested in Python, but we CAN guard the
module's structural invariants:

  * The file exists + parses with ``node --check``.
  * It exports both ``useSafeFetch`` and ``useSafePost`` (consumed by
    page migrations).
  * It uses ``isMountedRef`` (the actual leak fix — without that ref,
    the hook reduces to a thin wrapper and the rename of B6 wouldn't
    be earned).
  * The cleanup function in the useEffect SETS isMountedRef.current
    to false (prevents post-unmount setState).
  * It imports from the real ``api.js`` (not a local stub).

These guards are cheap CI signals — when someone "refactors" the hook
into something that no longer guards against unmounted-component
setState, the test fails.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "xmclaw" / "daemon" / "static" / "lib" / "use_safe_fetch.js"


def test_hook_file_exists() -> None:
    assert HOOK_PATH.is_file(), f"missing hook module: {HOOK_PATH}"


def test_hook_parses_with_node() -> None:
    """node --check the file parses as valid JS."""
    if not shutil.which("node"):
        pytest.skip("node not on PATH — CI must have it; local dev OK")
    r = subprocess.run(
        ["node", "--check", str(HOOK_PATH)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, f"node --check failed: {r.stderr}"


def test_exports_safe_fetch_and_safe_post() -> None:
    src = HOOK_PATH.read_text(encoding="utf-8")
    assert re.search(r"export\s+function\s+useSafeFetch\b", src), (
        "useSafeFetch export not found — page migrations depend on it"
    )
    assert re.search(r"export\s+function\s+useSafePost\b", src), (
        "useSafePost export not found"
    )


def test_uses_is_mounted_ref_guard() -> None:
    """The whole point of B6 is the isMounted guard. If a future
    refactor drops the ref, this test catches it."""
    src = HOOK_PATH.read_text(encoding="utf-8")
    assert "isMountedRef" in src, (
        "isMountedRef gone — leak guard removed (audit B6 regression)"
    )
    # Expect at least one ``isMountedRef.current`` access (the actual
    # check that gates setState calls).
    accesses = re.findall(r"isMountedRef\.current", src)
    assert len(accesses) >= 4, (
        f"expected multiple isMountedRef.current accesses (set true on "
        f"mount, set false on unmount, gate setData/setError/setLoading); "
        f"only found {len(accesses)}. B6 regression suspected."
    )


def test_cleanup_sets_unmounted() -> None:
    """The useEffect cleanup must set isMountedRef.current = false."""
    src = HOOK_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"isMountedRef\.current\s*=\s*false", src,
    ), "cleanup function missing the unmount-flip — leak still possible"


def test_imports_real_api_module() -> None:
    """Import from ../lib/api.js (not a local stub) so ``token`` /
    error handling is consistent with the rest of the UI."""
    src = HOOK_PATH.read_text(encoding="utf-8")
    assert re.search(
        r'from\s+"\./api\.js"', src,
    ), "must import apiGet/apiSend from ./api.js (the canonical surface)"


def test_uses_use_callback_for_stable_refresh() -> None:
    """``refresh`` must be a stable callback (useCallback) so consumers
    can put it in dependency arrays without infinite re-renders."""
    src = HOOK_PATH.read_text(encoding="utf-8")
    assert "useCallback" in src, (
        "useCallback missing — refresh would re-create every render and "
        "trigger consumer re-fetches"
    )
