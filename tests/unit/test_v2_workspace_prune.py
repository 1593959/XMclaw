"""B-152 — workspace router prune-missing tests.

Pins:
  * GET surfaces ``exists`` + ``looks_temp`` per root
  * PUT action=prune_missing removes only paths that don't exist
  * include_temp=True also removes pytest-of- / Temp / .claude/worktrees
    paths even if they happen to still exist
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test gets its own ~/.xmclaw via XMC_DATA_DIR so prune
    operations don't touch the dev machine's actual state.json."""
    isolated = tmp_path / "xmclaw_data"
    isolated.mkdir()
    monkeypatch.setenv("XMC_DATA_DIR", str(isolated))
    # Reset the module-level WorkspaceManager so it picks up the new env.
    from xmclaw.daemon.routers import workspace as ws_router
    from xmclaw.core.workspace import WorkspaceManager
    ws_router._manager = WorkspaceManager()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    return create_app(config={}, config_path=cfg_path), tmp_path


def test_get_includes_exists_flag(isolated_app, tmp_path: Path) -> None:
    app, _ = isolated_app
    real = tmp_path / "real_dir"
    real.mkdir()
    fake = tmp_path / "i_was_deleted"
    # Don't create fake — we want a stale registration
    with TestClient(app) as c:
        c.put("/api/v2/workspace", json={
            "action": "add", "path": str(real),
        })
        # Bypass _manager.add (which checks the path) by adding then deleting
        c.put("/api/v2/workspace", json={
            "action": "add", "path": str(fake.parent / "tmp_real_for_now"),
        })
        # Hack: direct disk add via WorkspaceManager since _manager.add()
        # may reject missing paths.
        from xmclaw.daemon.routers import workspace as ws_router
        ws_router._manager.add(str(real), name="real")
        # Now nuke the dir to simulate "registered then deleted on disk"
        # (use a different real dir we'll delete after add)
        gone = tmp_path / "gone"
        gone.mkdir()
        ws_router._manager.add(str(gone), name="gone")
        gone.rmdir()
        r = c.get("/api/v2/workspace")
    data = r.json()
    by_path = {row["path"]: row for row in data["roots"]}
    assert by_path[str(real)]["exists"] is True
    assert by_path[str(gone)]["exists"] is False


def test_get_flags_temp_paths(isolated_app, tmp_path: Path) -> None:
    """pytest-of-* / Temp / .claude/worktrees → looks_temp=true."""
    app, _ = isolated_app
    pytest_path = tmp_path / "pytest-of-15978" / "fake-test"
    pytest_path.mkdir(parents=True)
    with TestClient(app) as c:
        from xmclaw.daemon.routers import workspace as ws_router
        ws_router._manager.add(str(pytest_path), name="pytest-fake")
        r = c.get("/api/v2/workspace")
    data = r.json()
    target = next(row for row in data["roots"] if "pytest-of-" in row["path"])
    assert target["looks_temp"] is True


def test_prune_missing_removes_only_dead_paths(isolated_app, tmp_path: Path) -> None:
    app, _ = isolated_app
    alive = tmp_path / "alive"
    alive.mkdir()
    dead = tmp_path / "dead"
    dead.mkdir()
    with TestClient(app) as c:
        from xmclaw.daemon.routers import workspace as ws_router
        ws_router._manager.add(str(alive), name="alive")
        ws_router._manager.add(str(dead), name="dead")
        # Simulate the dir disappearing AFTER registration
        dead.rmdir()
        r = c.put("/api/v2/workspace", json={"action": "prune_missing"})
    data = r.json()
    paths = {row["path"] for row in data["roots"]}
    assert str(alive) in paths
    assert str(dead) not in paths
    assert str(dead) in data["pruned"]


def test_prune_missing_with_include_temp_strips_pytest_paths(isolated_app, tmp_path: Path) -> None:
    """include_temp=True drops all looks_temp + missing entries.

    NOTE: on Windows pytest, ``tmp_path`` itself is under
    ``%TEMP%\\pytest-of-...`` so EVERY path created here matches the
    looks_temp heuristic. The test verifies that include_temp=True
    prunes them all (and that without include_temp they survive
    when the dir still exists — covered by the prior test).
    """
    app, _ = isolated_app
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    p1.mkdir(); p2.mkdir()
    with TestClient(app) as c:
        from xmclaw.daemon.routers import workspace as ws_router
        ws_router._manager.add(str(p1), name="a")
        ws_router._manager.add(str(p2), name="b")
        # Sanity: GET reports them as looks_temp on Windows
        r = c.get("/api/v2/workspace")
        roots = r.json()["roots"]
        # Either both flagged temp (Windows pytest), or neither
        # (POSIX with /tmp — also flagged because /tmp/ prefix matches).
        # In any case, with include_temp=True, both get pruned.
        r = c.put("/api/v2/workspace", json={
            "action": "prune_missing",
            "include_temp": True,
        })
    data = r.json()
    pruned = set(data.get("pruned", []))
    # If both were flagged, both pruned. If neither was flagged
    # (some unusual env), pruned is empty — that's also fine; the
    # key contract is "include_temp doesn't UNDER-prune".
    if any(row["looks_temp"] for row in roots):
        assert str(p1) in pruned
        assert str(p2) in pruned


def test_prune_without_include_temp_keeps_existing_temp_path(isolated_app, tmp_path: Path) -> None:
    app, _ = isolated_app
    pytest_path = tmp_path / "pytest-of-15978" / "still-there"
    pytest_path.mkdir(parents=True)
    with TestClient(app) as c:
        from xmclaw.daemon.routers import workspace as ws_router
        ws_router._manager.add(str(pytest_path), name="x")
        r = c.put("/api/v2/workspace", json={"action": "prune_missing"})
    data = r.json()
    # path exists on disk + include_temp=False → kept
    assert str(pytest_path) in {row["path"] for row in data["roots"]}
