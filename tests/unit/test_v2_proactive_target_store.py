"""Sprint 2 Wave 10 — ProactiveTargetStore unit tests.

JSON-backed persistent registry of (channel, chat_ref) tuples that
users opt into via the /订阅 slash command. Covers:

  * load_targets returns [] on missing file
  * add_target persists + returns True on first add, False on dup
  * remove_target returns False when not present
  * Corrupted JSON file → empty list (no crash)
  * Concurrent add_target serialized by lock (no lost writes)
  * Empty / whitespace ref rejected
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from xmclaw.cognition.proactive_target_store import (
    add_target,
    load_targets,
    remove_target,
)


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    return tmp_path / "proactive_targets.json"


def test_load_targets_missing_file_returns_empty(tmp_store: Path) -> None:
    assert load_targets(tmp_store) == []


def test_load_targets_corrupted_json_returns_empty(tmp_store: Path) -> None:
    tmp_store.write_text("not-valid-json{{", encoding="utf-8")
    assert load_targets(tmp_store) == []


def test_load_targets_wrong_root_type_returns_empty(
    tmp_store: Path,
) -> None:
    tmp_store.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_targets(tmp_store) == []


def test_load_targets_filters_invalid_items(tmp_store: Path) -> None:
    tmp_store.write_text(
        json.dumps({
            "version": 1,
            "targets": [
                {"channel": "feishu", "ref": "oc_good", "added_ts": 1.0},
                {"channel": "feishu", "ref": "", "added_ts": 1.0},  # bad
                {"ref": "oc_no_channel"},                            # bad
                "string-not-dict",                                    # bad
            ],
        }),
        encoding="utf-8",
    )
    out = load_targets(tmp_store)
    assert len(out) == 1
    assert out[0].channel == "feishu"
    assert out[0].ref == "oc_good"


@pytest.mark.asyncio
async def test_add_target_persists_and_dedupes(tmp_store: Path) -> None:
    first = await add_target("feishu", "oc_abc", path=tmp_store)
    assert first is True
    second = await add_target("feishu", "oc_abc", path=tmp_store)
    assert second is False
    targets = load_targets(tmp_store)
    assert len(targets) == 1


@pytest.mark.asyncio
async def test_add_target_trims_whitespace(tmp_store: Path) -> None:
    await add_target("feishu", "  oc_xyz  ", path=tmp_store)
    targets = load_targets(tmp_store)
    assert targets[0].ref == "oc_xyz"


@pytest.mark.asyncio
async def test_add_target_rejects_empty(tmp_store: Path) -> None:
    assert await add_target("feishu", "", path=tmp_store) is False
    assert await add_target("feishu", "   ", path=tmp_store) is False
    assert await add_target("", "oc_abc", path=tmp_store) is False
    assert load_targets(tmp_store) == []


@pytest.mark.asyncio
async def test_remove_target_returns_false_when_absent(
    tmp_store: Path,
) -> None:
    assert (
        await remove_target("feishu", "oc_nope", path=tmp_store)
    ) is False


@pytest.mark.asyncio
async def test_remove_target_removes(tmp_store: Path) -> None:
    await add_target("feishu", "oc_x", path=tmp_store)
    await add_target("feishu", "oc_y", path=tmp_store)
    assert (
        await remove_target("feishu", "oc_x", path=tmp_store)
    ) is True
    remaining = load_targets(tmp_store)
    assert len(remaining) == 1
    assert remaining[0].ref == "oc_y"


@pytest.mark.asyncio
async def test_concurrent_adds_no_lost_writes(tmp_store: Path) -> None:
    """asyncio.gather of 10 concurrent adds must end with all 10
    distinct targets persisted — no lost writes from the read-modify-
    write race."""
    refs = [f"oc_{i:02d}" for i in range(10)]
    await asyncio.gather(*(
        add_target("feishu", r, path=tmp_store) for r in refs
    ))
    saved = load_targets(tmp_store)
    saved_refs = sorted(t.ref for t in saved)
    assert saved_refs == sorted(refs)


@pytest.mark.asyncio
async def test_multiple_channels_isolated(tmp_store: Path) -> None:
    await add_target("feishu", "oc_x", path=tmp_store)
    await add_target("telegram", "oc_x", path=tmp_store)  # same ref, diff channel
    targets = load_targets(tmp_store)
    assert len(targets) == 2
    assert {t.channel for t in targets} == {"feishu", "telegram"}
