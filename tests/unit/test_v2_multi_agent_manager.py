"""MultiAgentManager — the registry of running Workspaces (Epic #17 Phase 2).

Locks in the three properties Phase 3 depends on:
  * concurrency-safe mutation (pending-starts dedup)
  * crash-safe persistence (atomic write, rehydrate on load)
  * inert-until-used (no hidden wiring into app.state)

Uses pytest-asyncio for the async entry points; falls back to
``anyio`` semantics only where a concurrency test needs them.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.multi_agent_manager import (
    AgentIdError,
    MultiAgentManager,
    _sanitize_id,
)
from xmclaw.daemon.workspace import Workspace


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def registry_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def manager(bus: InProcessEventBus, registry_dir: Path) -> MultiAgentManager:
    return MultiAgentManager(bus, registry_dir=registry_dir)


@pytest.fixture
def llm_config() -> dict[str, object]:
    return {
        "llm": {
            "anthropic": {
                "api_key": "sk-ant-test",
                "default_model": "claude-haiku-4-5",
            },
        },
    }


# ── _sanitize_id ─────────────────────────────────────────────────────────


def test_sanitize_preserves_safe_id() -> None:
    assert _sanitize_id("agent-1_main") == "agent-1_main"


def test_sanitize_replaces_unsafe_with_underscore() -> None:
    assert _sanitize_id("a/b") == "a_b"
    assert _sanitize_id("..\\win") == "___win"
    assert _sanitize_id("x y") == "x_y"


def test_sanitize_empty_becomes_default() -> None:
    assert _sanitize_id("") == "default"


# ── read-only views start empty ──────────────────────────────────────────


def test_empty_manager_lists_nothing(manager: MultiAgentManager) -> None:
    assert manager.list_ids() == []
    assert len(manager) == 0
    assert manager.get("missing") is None
    assert "missing" not in manager


# ── create: happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_registers_and_persists(
    manager: MultiAgentManager, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    ws = await manager.create("main", llm_config)

    assert isinstance(ws, Workspace)
    assert ws.agent_id == "main"
    assert ws.is_ready() is True

    assert manager.get("main") is ws
    assert manager.list_ids() == ["main"]
    assert "main" in manager
    assert len(manager) == 1

    cfg_path = registry_dir / "main.json"
    assert cfg_path.exists()
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["agent_id"] == "main"
    assert on_disk["llm"]["anthropic"]["default_model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_create_llm_less_agent_is_registered_but_not_ready(
    manager: MultiAgentManager, registry_dir: Path
) -> None:
    ws = await manager.create("blank", {})
    assert ws.is_ready() is False
    assert manager.get("blank") is ws
    assert (registry_dir / "blank.json").exists()


# ── create: dedup + idempotency ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_existing_id_returns_same_workspace(
    manager: MultiAgentManager, llm_config: dict[str, object]
) -> None:
    first = await manager.create("main", llm_config)
    second = await manager.create("main", llm_config)
    assert first is second
    assert len(manager) == 1


@pytest.mark.asyncio
async def test_create_concurrent_same_id_deduplicates(
    bus: InProcessEventBus, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    # Two simultaneous creates for the same ID must yield the same
    # AgentLoop — otherwise we leak a second provider client.
    manager = MultiAgentManager(bus, registry_dir=registry_dir)
    results = await asyncio.gather(
        manager.create("race", llm_config),
        manager.create("race", llm_config),
        manager.create("race", llm_config),
    )
    assert results[0] is results[1] is results[2]
    assert manager.list_ids() == ["race"]


@pytest.mark.asyncio
async def test_create_different_ids_are_independent(
    manager: MultiAgentManager, llm_config: dict[str, object]
) -> None:
    a = await manager.create("a", llm_config)
    b = await manager.create("b", llm_config)
    assert a is not b
    assert a.agent_id == "a"
    assert b.agent_id == "b"
    assert set(manager.list_ids()) == {"a", "b"}


# ── create: bad IDs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_empty_id(manager: MultiAgentManager) -> None:
    with pytest.raises(AgentIdError, match="non-empty"):
        await manager.create("", {})
    with pytest.raises(AgentIdError, match="non-empty"):
        await manager.create("   ", {})


@pytest.mark.asyncio
async def test_create_rejects_unsafe_id_rather_than_rewriting(
    manager: MultiAgentManager, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    # Silently sanitizing "a/b" to "a_b" would leave the caller thinking
    # they registered "a/b" while the file is "a_b.json" — the in-memory
    # key and the disk name would drift.
    with pytest.raises(AgentIdError, match="unsafe"):
        await manager.create("path/trav", llm_config)
    assert list(registry_dir.iterdir()) == []


# ── remove ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_deletes_from_registry_and_disk(
    manager: MultiAgentManager, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    await manager.create("kill-me", llm_config)
    cfg = registry_dir / "kill-me.json"
    assert cfg.exists()

    removed = await manager.remove("kill-me")
    assert removed is True
    assert manager.get("kill-me") is None
    assert not cfg.exists()


@pytest.mark.asyncio
async def test_remove_returns_false_when_missing(
    manager: MultiAgentManager,
) -> None:
    assert await manager.remove("never-registered") is False


@pytest.mark.asyncio
async def test_remove_is_idempotent(
    manager: MultiAgentManager, llm_config: dict[str, object]
) -> None:
    await manager.create("agent", llm_config)
    assert await manager.remove("agent") is True
    # Second call is a no-op — both dict and file are already gone.
    assert await manager.remove("agent") is False


# ── load_from_disk ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_from_disk_rehydrates_agents(
    bus: InProcessEventBus, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    # Simulate daemon restart: write configs directly, build a fresh
    # manager, call load_from_disk, expect both agents back.
    (registry_dir / "a1.json").write_text(
        json.dumps({"agent_id": "a1", **llm_config}), encoding="utf-8"
    )
    (registry_dir / "a2.json").write_text(
        json.dumps({"agent_id": "a2", **llm_config}), encoding="utf-8"
    )

    fresh = MultiAgentManager(bus, registry_dir=registry_dir)
    loaded = await fresh.load_from_disk()

    assert sorted(loaded) == ["a1", "a2"]
    assert set(fresh.list_ids()) == {"a1", "a2"}
    ws = fresh.get("a1")
    assert ws is not None
    assert ws.is_ready() is True


@pytest.mark.asyncio
async def test_load_from_disk_skips_malformed_files(
    bus: InProcessEventBus, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    (registry_dir / "good.json").write_text(
        json.dumps({"agent_id": "good", **llm_config}), encoding="utf-8"
    )
    (registry_dir / "broken.json").write_text("not json {{", encoding="utf-8")
    (registry_dir / "notobject.json").write_text('["list-at-top"]', encoding="utf-8")

    fresh = MultiAgentManager(bus, registry_dir=registry_dir)
    loaded = await fresh.load_from_disk()

    # A hand-edited bad file must not 500 the whole daemon boot.
    assert loaded == ["good"]
    assert fresh.list_ids() == ["good"]


@pytest.mark.asyncio
async def test_load_from_disk_handles_missing_dir(
    bus: InProcessEventBus, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist"
    fresh = MultiAgentManager(bus, registry_dir=missing)
    assert await fresh.load_from_disk() == []


@pytest.mark.asyncio
async def test_load_from_disk_preserves_llm_less_presets(
    bus: InProcessEventBus, registry_dir: Path
) -> None:
    (registry_dir / "no_llm.json").write_text(
        json.dumps({"agent_id": "no_llm"}), encoding="utf-8"
    )
    fresh = MultiAgentManager(bus, registry_dir=registry_dir)
    loaded = await fresh.load_from_disk()
    assert loaded == ["no_llm"]
    ws = fresh.get("no_llm")
    assert ws is not None
    assert ws.is_ready() is False


# ── persistence details ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_uses_atomic_write_no_tmp_lingers(
    manager: MultiAgentManager, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    await manager.create("atomic", llm_config)
    # The tmp-file + rename dance must clean up after itself — no
    # .*.tmp siblings lingering would trip load_from_disk next boot.
    leftover = list(registry_dir.glob(".*tmp*"))
    assert leftover == []


@pytest.mark.asyncio
async def test_create_then_load_round_trips(
    bus: InProcessEventBus, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    first = MultiAgentManager(bus, registry_dir=registry_dir)
    await first.create("persist", llm_config)

    second = MultiAgentManager(bus, registry_dir=registry_dir)
    loaded = await second.load_from_disk()
    assert loaded == ["persist"]
    reloaded = second.get("persist")
    assert reloaded is not None
    assert reloaded.agent_id == "persist"
    assert reloaded.is_ready() is True


@pytest.mark.asyncio
async def test_max_hops_propagates_to_loaded_agents(
    bus: InProcessEventBus, registry_dir: Path, llm_config: dict[str, object]
) -> None:
    first = MultiAgentManager(bus, registry_dir=registry_dir, max_hops=5)
    await first.create("a", llm_config)

    second = MultiAgentManager(bus, registry_dir=registry_dir, max_hops=9)
    await second.load_from_disk()
    ws = second.get("a")
    assert ws is not None and ws.agent_loop is not None
    assert ws.agent_loop._max_hops == 9


# ── Phase 7: evolution-kind workspaces ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_evolution_workspace_starts_observer(
    manager: MultiAgentManager,
) -> None:
    # The manager must call ws.start() before publishing the workspace —
    # otherwise a caller that races create+list could see the workspace
    # without its bus subscription installed.
    ws = await manager.create("evo-1", {"kind": "evolution"})
    assert ws.kind == "evolution"
    assert ws.observer is not None
    assert ws.observer.is_running() is True


@pytest.mark.asyncio
async def test_remove_evolution_workspace_stops_observer(
    manager: MultiAgentManager,
) -> None:
    ws = await manager.create("evo-1", {"kind": "evolution"})
    observer = ws.observer
    assert observer is not None
    assert observer.is_running() is True
    assert await manager.remove("evo-1") is True
    assert observer.is_running() is False


@pytest.mark.asyncio
async def test_load_from_disk_starts_evolution_observers(
    bus: InProcessEventBus, registry_dir: Path,
) -> None:
    (registry_dir / "evo-1.json").write_text(
        json.dumps({"agent_id": "evo-1", "kind": "evolution"}),
        encoding="utf-8",
    )
    fresh = MultiAgentManager(bus, registry_dir=registry_dir)
    loaded = await fresh.load_from_disk()
    assert loaded == ["evo-1"]
    ws = fresh.get("evo-1")
    assert ws is not None
    assert ws.kind == "evolution"
    assert ws.observer is not None
    assert ws.observer.is_running() is True
