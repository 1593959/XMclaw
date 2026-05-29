"""Epic #27 sweep #9 (2026-05-19): persona-write inner timeout.

``remember`` / ``learn_about_user`` / ``memory_pin`` all funnel
through :meth:`BuiltinToolsPersonaMixin._append_persona`. Pre-fix the
only timeout was the hop_loop outer 180s wall-clock — if the
underlying PersonaStore.set_manual (embedding pipeline / vec_db /
sqlite) wedged, the tool sat 3 minutes per call and daemon.log
showed 56 such timeouts per day on the user's machine. Each was a
user-visible UI freeze.

The new path wraps the body in ``asyncio.wait_for(_, 30.0)`` and
returns a clean ``ok=False`` ToolResult on timeout instead of
letting the outer 180s gate strike.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin_persona import (
    BuiltinToolsPersonaMixin,
)


class _SlowStore:
    """PersonaStore stub that hangs on set_manual — simulates the
    "embedding pipeline stuck" production case."""

    async def read_manual(self, _basename: str) -> str:
        return ""

    async def set_manual(
        self, _basename: str, _text: str, **_kwargs: object,
    ) -> None:
        # Sleep way past the inner timeout; if not wrapped, _append_persona
        # would block here for the full duration.
        # 2026-05-29 (B-8): ``**_kwargs`` swallow lets us stay forward-
        # compatible with new optional kwargs the real PersonaStore
        # grew (most recently ``render=...`` for write-time render
        # control). Pre-fix, this stub raised TypeError on the kwarg
        # and the surrounding test saw a ``store write failed`` error
        # instead of the ``timed out`` it was trying to assert on.
        await asyncio.sleep(60.0)


class _PersonaHarness(BuiltinToolsPersonaMixin):
    """Minimal harness so we can drive _append_persona in isolation."""

    def __init__(self, persona_dir: Path, store: Any = None) -> None:
        self._persona_dir = persona_dir
        self._persona_dir_provider = lambda: self._persona_dir
        self._persona_store_provider = (
            (lambda: store) if store is not None else None
        )
        # _fs_lock is provided by the mixin's __init__ in real code;
        # here we wire a real lock map so per-path serialisation works.
        self._locks: dict[str, asyncio.Lock] = {}
        # _persona_writeback exists on the real BuiltinTools subclass
        # for post-write system-prompt rebuild; harness doesn't need it.
        self._persona_writeback = None

    def _fs_lock(self, path: Path):  # noqa: ANN201
        key = str(path)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


@pytest.mark.asyncio
async def test_append_persona_returns_clean_error_on_inner_timeout(
    tmp_path: Path,
) -> None:
    """When the persona store hangs past the inner timeout, the
    tool returns ok=False with a clear error within ~the timeout
    window — NOT the 180s outer wall-clock."""
    harness = _PersonaHarness(tmp_path, store=_SlowStore())
    call = ToolCall(name="remember", args={}, provenance="test")
    start = time.perf_counter()
    # Use a short inner timeout for fast test.
    result = await harness._append_persona(
        call, time.perf_counter(),
        basename="MEMORY.md", section="Test", entry="payload",
        placeholder_title="MEMORY.md — what I want to remember next time",
        inner_timeout_s=0.5,
    )
    elapsed = time.perf_counter() - start
    assert result.ok is False
    assert "timed out" in (result.error or "").lower()
    # Must return within ~1s — not 60s (the simulated hang).
    assert elapsed < 5.0, (
        f"timeout wrapper failed; took {elapsed:.2f}s "
        f"(should have aborted at 0.5s)"
    )


@pytest.mark.asyncio
async def test_append_persona_fast_path_unaffected(tmp_path: Path) -> None:
    """Fast write completes well within the timeout window. The
    wrapper is a safety net, not a brake on the happy path."""
    harness = _PersonaHarness(tmp_path)  # no store → direct file write
    call = ToolCall(name="learn_about_user", args={}, provenance="test")
    result = await harness._append_persona(
        call, time.perf_counter(),
        basename="USER.md", section="Profile", entry="developer in CN",
        placeholder_title="USER.md — who I'm working with",
        inner_timeout_s=10.0,
    )
    assert result.ok is True
    # File on disk has the entry.
    assert (tmp_path / "USER.md").exists()
    text = (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "developer in CN" in text
