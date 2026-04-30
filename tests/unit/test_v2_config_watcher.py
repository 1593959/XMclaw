"""B-109: config_watcher hot-reload.

Pins:
  * tick() detects an external write and mutates the in-memory dict
  * malformed JSON skips this tick gracefully
  * unchanged file → no-op (mtime delta below threshold)
  * runtime-only diff → restart_required=False
  * llm.* diff → restart_required=True
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from xmclaw.daemon.config_watcher import ConfigFileWatcher, _diff_keys


# ── _diff_keys helper ──────────────────────────────────────────────────


def test_diff_keys_returns_changed_paths() -> None:
    old = {"llm": {"anthropic": {"api_key": "old"}}, "tools": {"enable_bash": True}}
    new = {"llm": {"anthropic": {"api_key": "new"}}, "tools": {"enable_bash": True}}
    assert _diff_keys(old, new) == ["llm.anthropic.api_key"]


def test_diff_keys_handles_added_and_removed() -> None:
    old = {"a": 1}
    new = {"b": 2}
    diffs = sorted(_diff_keys(old, new))
    assert diffs == ["a", "b"]


def test_diff_keys_treats_reordered_lists_as_unequal() -> None:
    """We use json.dumps(sort_keys=True) for comparison, but lists are
    NOT sorted — so [1,2] vs [2,1] DO count as a change. This is
    intentional: list order can be semantic in config (priority lists
    etc.). Pin so a future "smart compare" doesn't silently drop these."""
    old = {"x": [1, 2]}
    new = {"x": [2, 1]}
    assert _diff_keys(old, new) == ["x"]


# ── ConfigFileWatcher.tick ─────────────────────────────────────────────


def _write_cfg(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_tick_detects_change_and_mutates_dict(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    initial = {"llm": {"anthropic": {"api_key": "old"}}}
    _write_cfg(cfg_path, initial)
    live = dict(initial)
    cw = ConfigFileWatcher(config_path=cfg_path, cfg=live)

    # Modify file. Bump mtime explicitly for Windows where same-second
    # writes can collapse the timestamp.
    new_cfg = {"llm": {"anthropic": {"api_key": "rotated"}}}
    _write_cfg(cfg_path, new_cfg)
    fresh_mtime = cfg_path.stat().st_mtime + 2.0
    os.utime(cfg_path, (fresh_mtime, fresh_mtime))

    summary = await cw.tick()
    assert summary is not None
    assert "llm.anthropic.api_key" in summary["changed_keys"]
    assert summary["restart_required"] is True
    # In-place mutation — the same ``live`` dict object reflects the
    # rotated key.
    assert live["llm"]["anthropic"]["api_key"] == "rotated"


@pytest.mark.asyncio
async def test_tick_unchanged_returns_none(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    _write_cfg(cfg_path, {"a": 1})
    live = {"a": 1}
    cw = ConfigFileWatcher(config_path=cfg_path, cfg=live)
    # No mtime bump → no work.
    assert await cw.tick() is None


@pytest.mark.asyncio
async def test_tick_skips_malformed_json(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    _write_cfg(cfg_path, {"a": 1})
    live = {"a": 1}
    cw = ConfigFileWatcher(config_path=cfg_path, cfg=live)
    cfg_path.write_text("{not: valid json", encoding="utf-8")
    fresh = cfg_path.stat().st_mtime + 2.0
    os.utime(cfg_path, (fresh, fresh))
    # tick should NOT crash and should NOT mutate live dict.
    summary = await cw.tick()
    assert summary is None
    assert live == {"a": 1}


@pytest.mark.asyncio
async def test_tick_marks_runtime_only_when_only_tools_change(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    _write_cfg(cfg_path, {"tools": {"enable_bash": True}})
    live = {"tools": {"enable_bash": True}}
    cw = ConfigFileWatcher(config_path=cfg_path, cfg=live)
    _write_cfg(cfg_path, {"tools": {"enable_bash": False}})
    fresh = cfg_path.stat().st_mtime + 2.0
    os.utime(cfg_path, (fresh, fresh))
    summary = await cw.tick()
    assert summary is not None
    assert summary["restart_required"] is False
    assert summary["runtime_only"] is True
