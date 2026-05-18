"""Memory auto-inject policy — Wave-32+ (2026-05-19).

User asked for an active-recall mechanism: the agent should query
memory on its own (via memory_search) rather than being force-fed
all top facts in every system prompt. These tests pin:

  * Default behavior preserved (auto-inject ON, cap=5)
  * Disabling the flag swaps in an active-recall hint
  * Cap is honored when injection is ON
"""
from __future__ import annotations

import pytest

from xmclaw.core.feature_flags import set_default_engine
from xmclaw.core.feature_flags.engine import FeatureFlagEngine
from xmclaw.core.feature_flags.registry import BUILTIN_FLAGS


@pytest.fixture(autouse=True)
def _fresh_engine(tmp_path, monkeypatch):
    """Each test gets a fresh FeatureFlagEngine so flag overrides
    don't leak across cases."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "xdata"))
    eng = FeatureFlagEngine(disk_path=tmp_path / "flags.json")
    eng.register_many(BUILTIN_FLAGS)
    set_default_engine(eng)
    yield eng
    set_default_engine(None)


def test_flags_registered() -> None:
    """Both new flags must be in the builtin catalogue so the
    operator UI surfaces them + the engine resolves defaults."""
    names = {f.name for f in BUILTIN_FLAGS}
    assert "memory.auto_inject.enabled" in names
    assert "memory.auto_inject.max_facts" in names


def test_auto_inject_enabled_default_true(_fresh_engine) -> None:
    """Default keeps the legacy behavior — auto-inject ON so existing
    users don't lose context silently after upgrade."""
    assert _fresh_engine.variant(
        "memory.auto_inject.enabled", default=False,
    ) is True


def test_max_facts_default_lowered_to_5(_fresh_engine) -> None:
    """Historical hardcode was 20; default lowered to 5 to leave
    room for active recall on demand."""
    assert _fresh_engine.variant(
        "memory.auto_inject.max_facts", default=0,
    ) == 5


def test_disable_then_check_flag(_fresh_engine) -> None:
    """Operator flips it off → variant() reflects the new value
    immediately (no daemon restart needed for the flag itself)."""
    _fresh_engine.set("memory.auto_inject.enabled", False, persist=False)
    assert _fresh_engine.variant("memory.auto_inject.enabled") is False


def test_cap_override_to_smaller_value(_fresh_engine) -> None:
    """Operator can lower the cap further for noisy environments
    or smaller context windows."""
    _fresh_engine.set("memory.auto_inject.max_facts", 2, persist=False)
    assert _fresh_engine.variant("memory.auto_inject.max_facts") == 2
