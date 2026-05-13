"""Static-scan tests for the Wave 13 sync client lib.

Mirrors test_v2_voice_loop.py: verify the module exports + endpoint
constant + debounce contract.
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "static"
)


def read(rel: str) -> str:
    return (STATIC_DIR / rel).read_text(encoding="utf-8")


def test_sync_module_exists_and_exports() -> None:
    src = read("lib/sync.js")
    for sym in (
        "export async function fetchUiState",
        "export async function putUiState",
        "export async function patchUiState",
        "export function debouncedPatch",
        "export async function flushPending",
    ):
        assert sym in src, f"missing export: {sym}"


def test_sync_uses_correct_endpoint() -> None:
    src = read("lib/sync.js")
    assert "/api/v2/sync/ui-state" in src


def test_sync_debounce_ms_constant() -> None:
    """500ms — fast enough that "rapid dropdown click" still feels
    consistent, slow enough that 1 round-trip absorbs 10 rapid changes."""
    src = read("lib/sync.js")
    assert "DEBOUNCE_MS = 500" in src


def test_sync_imports_api_helpers() -> None:
    """Don't reimplement fetch — use the existing apiGet/apiSend so
    pairing-token auth and content-type headers stay consistent."""
    src = read("lib/sync.js")
    assert "from \"./api.js\"" in src
    assert "apiGet" in src
    assert "apiSend" in src


def test_sync_swallows_patch_failures() -> None:
    """Sync is best-effort. A backend hiccup should NOT bubble up
    and crash the UI."""
    src = read("lib/sync.js")
    assert "try {" in src
    assert "console.warn" in src
    assert "patch failed" in src
