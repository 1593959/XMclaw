"""Tests for Phase 3 — workspace API + multi-agent 4 conventions."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from xmclaw.core.multi_agent import (
    AgentContext,
    AgentNotFound,
    MultiAgentManager,
    current_agent_id,
    set_current_agent_id,
    reset_current_agent_id,
)
from xmclaw.core.workspace import (
    WorkspaceManager,
    WorkspaceRoot,
    WorkspaceState,
)
from xmclaw.core.workspace.types import detect_vcs


# ── Workspace types ───────────────────────────────────────────────────


def test_workspace_root_resolves_path(tmp_path):
    root = WorkspaceRoot.from_path(str(tmp_path))
    assert root.path.is_absolute()
    assert root.name == tmp_path.name


def test_workspace_root_round_trip_via_dict(tmp_path):
    r = WorkspaceRoot.from_path(str(tmp_path), name="myproject")
    raw = r.to_dict()
    assert raw["name"] == "myproject"
    rebuilt = WorkspaceRoot.from_dict(raw)
    assert rebuilt == r


def test_detect_vcs_finds_git(tmp_path):
    assert detect_vcs(tmp_path) == "none"
    (tmp_path / ".git").mkdir()
    assert detect_vcs(tmp_path) == "git"


def test_workspace_state_primary_clamps_to_valid_index(tmp_path):
    r1 = WorkspaceRoot.from_path(str(tmp_path / "a"))
    r2 = WorkspaceRoot.from_path(str(tmp_path / "b"))
    s = WorkspaceState(roots=[r1, r2], primary_index=99)
    assert s.primary == r2  # clamped


def test_workspace_state_primary_none_for_empty():
    s = WorkspaceState(roots=[], primary_index=0)
    assert s.primary is None


# ── WorkspaceManager ──────────────────────────────────────────────────


def _mgr(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(path=tmp_path / "state.json")


def test_manager_starts_empty(tmp_path):
    m = _mgr(tmp_path)
    assert m.get().roots == []
    assert m.get().primary_index == 0


def test_manager_add_persists_to_disk(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "proj"
    p.mkdir()
    root = m.add(p)
    state_file = tmp_path / "state.json"
    assert state_file.exists()
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw["roots"][0]["path"] == str(root.path)


def test_manager_add_dedups(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "proj"
    p.mkdir()
    a = m.add(p)
    b = m.add(p)
    assert a == b
    assert len(m.get().roots) == 1


def test_manager_remove(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "proj"
    p.mkdir()
    m.add(p)
    assert m.remove(p)
    assert m.get().roots == []
    assert not m.remove(p)  # second call returns False


def test_manager_set_primary(tmp_path):
    m = _mgr(tmp_path)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    m.add(a)
    m.add(b)
    # b was added last → primary == 1.
    assert m.get().primary_index == 1
    assert m.set_primary(0)
    assert m.get().primary_index == 0
    # No-op when already primary.
    assert not m.set_primary(0)


def test_manager_resolves_path_to_root(tmp_path):
    m = _mgr(tmp_path)
    p = tmp_path / "proj"; p.mkdir()
    (p / "src").mkdir()
    m.add(p)
    file_inside = p / "src" / "x.py"
    file_inside.touch()
    root = m.resolve_path_to_root(file_inside)
    assert root is not None and root.path == p.resolve()
    # Outside the workspace
    outside = tmp_path / "elsewhere"; outside.mkdir()
    assert m.resolve_path_to_root(outside) is None


def test_manager_atomic_write_under_corrupt_state(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not valid json{{", encoding="utf-8")
    m = WorkspaceManager(path=state_file)
    # Bad file should not crash; should treat as empty.
    assert m.get().roots == []
    p = tmp_path / "ok"; p.mkdir()
    m.add(p)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(raw["roots"]) == 1


# ── ContextVar ────────────────────────────────────────────────────────


def test_current_agent_id_default_is_main():
    assert current_agent_id() == "main"


def test_set_and_reset_agent_id():
    token = set_current_agent_id("coder")
    try:
        assert current_agent_id() == "coder"
    finally:
        reset_current_agent_id(token)
    assert current_agent_id() == "main"


def test_agent_context_manager_isolates_scope():
    with AgentContext("researcher"):
        assert current_agent_id() == "researcher"
        with AgentContext("inner"):
            assert current_agent_id() == "inner"
        assert current_agent_id() == "researcher"
    assert current_agent_id() == "main"


def test_async_context_does_not_leak_across_tasks():
    async def task(name: str, results: dict):
        with AgentContext(name):
            await asyncio.sleep(0)
            results[name] = current_agent_id()

    async def runner():
        results: dict = {}
        await asyncio.gather(
            task("a", results),
            task("b", results),
            task("c", results),
        )
        return results

    out = asyncio.run(runner())
    assert out == {"a": "a", "b": "b", "c": "c"}


# ── MultiAgentManager dedup ───────────────────────────────────────────


def test_manager_register_and_peek():
    mam = MultiAgentManager[str]()
    mam.register("coder", "instance-1")
    assert mam.peek("coder") == "instance-1"
    assert mam.list_ids() == ["coder"]
    assert mam.has("coder")


def test_manager_get_unknown_raises_without_factory():
    mam = MultiAgentManager[str]()
    with pytest.raises(AgentNotFound):
        asyncio.run(mam.get("ghost"))


def test_manager_get_runs_factory_lazily():
    factory_calls: list[str] = []

    async def factory(agent_id):
        factory_calls.append(agent_id)
        await asyncio.sleep(0.01)
        return f"runtime-for-{agent_id}"

    mam = MultiAgentManager[str](factory=factory)

    async def runner():
        return await mam.get("coder")

    runtime = asyncio.run(runner())
    assert runtime == "runtime-for-coder"
    assert factory_calls == ["coder"]


def test_manager_get_dedups_concurrent_starts():
    """Convention #3: two concurrent gets for the same id share one factory call."""
    factory_calls: list[str] = []

    async def factory(agent_id):
        factory_calls.append(agent_id)
        await asyncio.sleep(0.05)  # simulate real construction
        return f"runtime-for-{agent_id}"

    mam = MultiAgentManager[str](factory=factory)

    async def runner():
        return await asyncio.gather(
            mam.get("coder"),
            mam.get("coder"),
            mam.get("coder"),
        )

    out = asyncio.run(runner())
    assert out == ["runtime-for-coder"] * 3
    # The factory must have run ONLY ONCE despite 3 concurrent requests.
    assert factory_calls == ["coder"]


def test_manager_get_distinct_agents_runs_in_parallel():
    """Distinct ids should not block each other on the manager lock."""
    factory_calls: list[str] = []

    async def factory(agent_id):
        factory_calls.append(agent_id)
        await asyncio.sleep(0.05)
        return f"r-{agent_id}"

    mam = MultiAgentManager[str](factory=factory)

    async def runner():
        return await asyncio.gather(
            mam.get("a"),
            mam.get("b"),
            mam.get("c"),
        )

    out = asyncio.run(runner())
    assert out == ["r-a", "r-b", "r-c"]
    assert sorted(factory_calls) == ["a", "b", "c"]


def test_manager_factory_failure_does_not_corrupt_state():
    async def bad_factory(agent_id):
        raise RuntimeError("kaboom")

    mam = MultiAgentManager[str](factory=bad_factory)

    async def runner():
        with pytest.raises(RuntimeError):
            await mam.get("coder")
        # Subsequent call should retry, not return a stale value.
        with pytest.raises(RuntimeError):
            await mam.get("coder")

    asyncio.run(runner())
    assert mam.peek("coder") is None
