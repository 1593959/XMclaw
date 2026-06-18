"""Feature-flag engine debounce tests.

Verifies that rapid set() / clear() calls are coalesced into a single
disk flush after a 500 ms debounce window.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xmclaw.core.feature_flags import FeatureFlag, FeatureFlagEngine


@pytest.fixture
def engine(tmp_path: Path):
    """Fresh engine with isolated disk."""
    return FeatureFlagEngine(disk_path=tmp_path / "features.json")


@pytest.mark.asyncio
async def test_high_freq_set_writes_once(engine) -> None:
    """5 rapid set() calls within 500 ms must result in a single _save_disk()."""
    with patch.object(engine, "_save_disk") as mock_save:
        engine.set("a", 1)
        engine.set("b", 2)
        engine.set("c", 3)
        engine.set("d", 4)
        engine.set("e", 5)
        # Wait for debounce window + a little margin
        await asyncio.sleep(0.6)
    assert mock_save.call_count == 1


@pytest.mark.asyncio
async def test_data_persisted_after_debounce(engine, tmp_path: Path) -> None:
    """After the debounce window expires, the disk file must contain the latest values."""
    engine.register(FeatureFlag("x.knob", default="def"))
    engine.set("x.knob", "updated")
    assert not (tmp_path / "features.json").exists()
    await asyncio.sleep(0.6)
    data = json.loads((tmp_path / "features.json").read_text())
    assert data["x.knob"] == "updated"


@pytest.mark.asyncio
async def test_clear_also_debounced(engine, tmp_path: Path) -> None:
    """clear() must participate in the same debounce schedule, not flush immediately."""
    engine.register(FeatureFlag("x.knob", default="def"))
    engine.set("x.knob", "to-be-cleared")
    # Wait for first debounce to finish so the file is written
    await asyncio.sleep(0.6)

    with patch.object(engine, "_save_disk", wraps=engine._save_disk) as mock_save:
        engine.clear("x.knob")
        await asyncio.sleep(0.6)
    assert mock_save.call_count == 1

    # And disk should be empty after the flush
    data = json.loads((tmp_path / "features.json").read_text())
    assert "x.knob" not in data
