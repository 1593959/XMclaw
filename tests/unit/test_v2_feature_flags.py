"""Feature-flag engine — 5-layer resolution priority tests.

Covers the canonical priority order (env > memory > disk > remote >
default) + persistence + the snapshot view used by the operator
UI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xmclaw.core.feature_flags import (
    FeatureFlag,
    FeatureFlagEngine,
    NoopRemoteProvider,
    set_default_engine,
)


# ── helpers ────────────────────────────────────────────────────────


class _FakeRemote:
    """RemoteProvider that returns a fixed dict + records calls."""

    def __init__(self, table: dict[str, object] | None = None) -> None:
        self.table = dict(table or {})
        self.lookups: list[str] = []

    def lookup(self, name: str):  # noqa: ANN201
        self.lookups.append(name)
        return self.table.get(name)


@pytest.fixture
def engine(tmp_path: Path):
    """Fresh engine per test, isolated disk + noop remote."""
    e = FeatureFlagEngine(disk_path=tmp_path / "features.json")
    set_default_engine(None)  # don't let module singleton leak
    return e


# ── priority order ────────────────────────────────────────────────


def test_default_layer_when_nothing_else_set(engine) -> None:
    engine.register(FeatureFlag("x.knob", default=42))
    assert engine.variant("x.knob") == 42


def test_remote_overrides_default(tmp_path: Path) -> None:
    remote = _FakeRemote({"x.knob": "remote-val"})
    e = FeatureFlagEngine(disk_path=tmp_path / "f.json", remote=remote)
    e.register(FeatureFlag("x.knob", default="default-val"))
    assert e.variant("x.knob") == "remote-val"


def test_disk_overrides_remote(tmp_path: Path) -> None:
    remote = _FakeRemote({"x.knob": "from-remote"})
    e = FeatureFlagEngine(disk_path=tmp_path / "f.json", remote=remote)
    e.register(FeatureFlag("x.knob", default="from-default"))
    e.set("x.knob", "from-disk")
    # Persistence test: rebuild engine, disk still has it.
    e2 = FeatureFlagEngine(disk_path=tmp_path / "f.json", remote=remote)
    e2.register(FeatureFlag("x.knob", default="from-default"))
    assert e2.variant("x.knob") == "from-disk"


def test_memory_overrides_disk(engine) -> None:
    engine.register(FeatureFlag("x.knob", default="default"))
    # Stash on disk via persist=True (the default).
    engine.set("x.knob", "disk-value")
    # Now override in-memory only.
    engine.set("x.knob", "memory-only", persist=False)
    assert engine.variant("x.knob") == "memory-only"


def test_env_overrides_memory(engine, monkeypatch) -> None:
    engine.register(FeatureFlag("x.knob", default="default"))
    engine.set("x.knob", "memory")
    monkeypatch.setenv("XMC_FF_X_KNOB", '"env-wins"')  # JSON string
    assert engine.variant("x.knob") == "env-wins"


def test_env_parses_json_typed(engine, monkeypatch) -> None:
    monkeypatch.setenv("XMC_FF_BOOL_FLAG", "true")
    monkeypatch.setenv("XMC_FF_INT_FLAG", "42")
    monkeypatch.setenv("XMC_FF_LIST_FLAG", "[1, 2, 3]")
    monkeypatch.setenv("XMC_FF_RAW_STRING", "not-json-just-text")
    assert engine.variant("bool.flag") is True
    assert engine.variant("int.flag") == 42
    assert engine.variant("list.flag") == [1, 2, 3]
    assert engine.variant("raw.string") == "not-json-just-text"


# ── is_enabled sugar ──────────────────────────────────────────────


def test_is_enabled_returns_true_for_truthy(engine) -> None:
    engine.set("x.on", True)
    engine.set("x.off", False)
    engine.set("x.truthy_str", "yes")
    engine.set("x.falsy_str", "")
    assert engine.is_enabled("x.on") is True
    assert engine.is_enabled("x.off") is False
    assert engine.is_enabled("x.truthy_str") is True
    assert engine.is_enabled("x.falsy_str") is False


def test_is_enabled_caller_default_when_unregistered(engine) -> None:
    assert engine.is_enabled("does.not.exist", default=True) is True
    assert engine.is_enabled("does.not.exist", default=False) is False


# ── set / clear ────────────────────────────────────────────────────


def test_clear_removes_from_memory_and_disk(engine) -> None:
    engine.register(FeatureFlag("x.knob", default="def"))
    engine.set("x.knob", "override")
    assert engine.variant("x.knob") == "override"
    engine.clear("x.knob")
    assert engine.variant("x.knob") == "def"
    # Disk also cleared — rebuild engine to verify.
    new = FeatureFlagEngine(disk_path=engine._disk_path)
    new.register(FeatureFlag("x.knob", default="def"))
    assert new.variant("x.knob") == "def"


def test_persist_false_does_not_touch_disk(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    e = FeatureFlagEngine(disk_path=p)
    e.register(FeatureFlag("x.knob", default="def"))
    e.set("x.knob", "memory-only", persist=False)
    # Disk file shouldn't even exist (or shouldn't contain the key).
    if p.exists():
        data = json.loads(p.read_text())
        assert "x.knob" not in data


# ── refresh ────────────────────────────────────────────────────────


def test_refresh_pulls_from_remote_into_memory(tmp_path: Path) -> None:
    remote = _FakeRemote({"x.a": "v1", "x.b": "v2"})
    e = FeatureFlagEngine(disk_path=tmp_path / "f.json", remote=remote)
    e.register_many([FeatureFlag("x.a"), FeatureFlag("x.b")])
    n = e.refresh()
    assert n == 2
    assert e.variant("x.a") == "v1"
    assert e.variant("x.b") == "v2"


def test_refresh_does_not_persist_to_disk(tmp_path: Path) -> None:
    """Remote values are ephemeral — they're cached in memory but
    NOT persisted. Otherwise a remote rollback wouldn't be visible
    to the next daemon restart."""
    p = tmp_path / "f.json"
    remote = _FakeRemote({"x.a": "from-remote"})
    e = FeatureFlagEngine(disk_path=p, remote=remote)
    e.register(FeatureFlag("x.a"))
    e.refresh()
    assert e.variant("x.a") == "from-remote"
    # Build a fresh engine without the remote — disk has nothing.
    e2 = FeatureFlagEngine(disk_path=p)
    e2.register(FeatureFlag("x.a", default="fallback"))
    assert e2.variant("x.a") == "fallback"


def test_refresh_remote_exception_isolated(engine) -> None:
    """A broken remote.lookup() mustn't break the engine."""
    class _Bad:
        def lookup(self, name):
            raise RuntimeError("network down")
    e = FeatureFlagEngine(disk_path=engine._disk_path, remote=_Bad())
    e.register(FeatureFlag("x.a", default="def"))
    assert e.variant("x.a") == "def"
    n = e.refresh()
    assert n == 0  # no successful pulls but no crash


# ── snapshot view (operator UI) ────────────────────────────────────


def test_snapshot_reports_layer_per_flag(tmp_path: Path, monkeypatch) -> None:
    """Each snapshot entry tells the operator WHICH layer resolved.

    Layer priority: env > memory > disk > remote > default. The
    snapshot reports the FIRST layer that produced a value, which
    matches the resolver's priority.

    For the ``from_disk`` case we have to construct it carefully:
    a normal ``set(persist=True)`` writes BOTH memory and disk, so
    the snapshot would honestly report ``memory`` (priority order).
    To exercise the disk layer in isolation we write the on-disk
    file via a separate engine, then re-construct so the new
    engine's memory cache is empty.
    """
    p = tmp_path / "f.json"
    # Seed disk via a throwaway engine.
    seed = FeatureFlagEngine(disk_path=p)
    seed.register(FeatureFlag("x.from_disk", default="seed-default"))
    seed.set("x.from_disk", "disk-val")  # writes disk + seed's memory

    # Real engine for the test — disk loaded fresh, memory empty.
    remote = _FakeRemote({"x.from_remote": "remote-val"})
    e = FeatureFlagEngine(disk_path=p, remote=remote)
    e.register_many([
        FeatureFlag("x.from_env"),
        FeatureFlag("x.from_memory"),
        FeatureFlag("x.from_disk"),
        FeatureFlag("x.from_remote"),
        FeatureFlag("x.from_default", default="def-val"),
    ])
    monkeypatch.setenv("XMC_FF_X_FROM_ENV", "env-val")
    e.set("x.from_memory", "mem-val", persist=False)
    snap = e.snapshot()
    assert snap["x.from_env"]["layer"] == "env"
    assert snap["x.from_env"]["value"] == "env-val"
    assert snap["x.from_memory"]["layer"] == "memory"
    assert snap["x.from_memory"]["value"] == "mem-val"
    assert snap["x.from_disk"]["layer"] == "disk"
    assert snap["x.from_disk"]["value"] == "disk-val"
    assert snap["x.from_remote"]["layer"] == "remote"
    assert snap["x.from_remote"]["value"] == "remote-val"
    assert snap["x.from_default"]["layer"] == "default"
    assert snap["x.from_default"]["value"] == "def-val"


# ── env name convention ───────────────────────────────────────────


def test_env_var_name_replaces_dots_and_dashes() -> None:
    flag = FeatureFlag("cognition.idle-aware_scheduling")
    assert flag.env_var() == "XMC_FF_COGNITION_IDLE_AWARE_SCHEDULING"
