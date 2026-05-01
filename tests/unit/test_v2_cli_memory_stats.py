"""``xmclaw memory stats`` CLI — surfaces the SqliteVecMemory.stats() data.

Epic #5 phase 3. Operator-facing: shows per-layer count/bytes/pinned/age
range for the memory store. Non-mutating — must never auto-create a DB.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from typer.testing import CliRunner

from xmclaw.cli.main import app
from xmclaw.providers.memory.base import MemoryItem
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


def _seed_db(db_path: Path) -> None:
    """Populate a small multi-layer DB so the CLI has something to show."""
    mem = SqliteVecMemory(db_path)
    try:
        asyncio.run(mem.put(
            "short",
            MemoryItem(
                id="s1", layer="short", text="hello",
                metadata={}, ts=100.0,
            ),
        ))
        asyncio.run(mem.put(
            "short",
            MemoryItem(
                id="s2", layer="short", text="你好",  # 6 UTF-8 bytes
                metadata={"pinned": True}, ts=200.0,
            ),
        ))
        asyncio.run(mem.put(
            "working",
            MemoryItem(
                id="w1", layer="working", text="abc",
                metadata={}, ts=150.0,
            ),
        ))
    finally:
        mem.close()


def test_memory_stats_json_reports_per_layer_counts(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    _seed_db(db)

    runner = CliRunner()
    r = runner.invoke(app, ["memory", "stats", "--db", str(db), "--json"])
    assert r.exit_code == 0, r.output

    body = json.loads(r.output)
    assert body["ok"] is True
    assert body["exists"] is True
    assert body["db_path"] == str(db)

    layers = body["layers"]
    assert set(layers) == {"short", "working", "long"}
    assert layers["short"]["count"] == 2
    assert layers["short"]["bytes"] == 5 + 6  # "hello" + "你好"
    assert layers["short"]["pinned_count"] == 1
    assert layers["short"]["oldest_ts"] == 100.0
    assert layers["short"]["newest_ts"] == 200.0
    assert layers["working"]["count"] == 1
    assert layers["long"]["count"] == 0


def test_memory_stats_text_shows_all_three_layers(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    _seed_db(db)

    runner = CliRunner()
    r = runner.invoke(app, ["memory", "stats", "--db", str(db)])
    assert r.exit_code == 0, r.output

    # Header + three layer rows always present, empty long row included.
    assert "layer" in r.output
    assert "short" in r.output
    assert "working" in r.output
    assert "long" in r.output
    # Pinned count surfaces.
    assert "pinned" in r.output


def test_memory_stats_missing_db_reports_cleanly(tmp_path: Path) -> None:
    """Fresh install / no memory yet: cleanly say so, don't crash."""
    db = tmp_path / "does_not_exist.db"
    assert not db.exists()

    runner = CliRunner()
    r = runner.invoke(app, ["memory", "stats", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "no memory DB" in r.output
    # And MUST NOT have silently created it.
    assert not db.exists()


def test_memory_stats_missing_db_json_reports_exists_false(tmp_path: Path) -> None:
    db = tmp_path / "does_not_exist.db"
    runner = CliRunner()
    r = runner.invoke(app, ["memory", "stats", "--db", str(db), "--json"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["ok"] is True
    assert body["exists"] is False
    assert body["layers"] == {}
    assert not db.exists()


def test_memory_stats_default_db_uses_home_workspace(tmp_path: Path) -> None:
    """With no --db, the command reads ~/.xmclaw/v2/memory.db.

    Verified negatively: a fresh HOME with no memory.db reports
    'no memory DB' pointing at that default path.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_vars = {
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    old = {k: os.environ.get(k) for k in env_vars}
    os.environ.update(env_vars)
    try:
        runner = CliRunner()
        r = runner.invoke(app, ["memory", "stats"])
        assert r.exit_code == 0, r.output
        assert "no memory DB" in r.output
        # Must print the default location — not tmp_path itself, but a
        # .xmclaw/v2/memory.db beneath the HOME override.
        assert ".xmclaw" in r.output
        assert "memory.db" in r.output
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_memory_stats_empty_db_still_reports_zeros(tmp_path: Path) -> None:
    """An existing but empty DB yields three zero rows, not 'missing'."""
    db = tmp_path / "empty.db"
    # Create an empty SqliteVec DB by opening and closing once.
    SqliteVecMemory(db).close()
    assert db.exists()

    runner = CliRunner()
    r = runner.invoke(app, ["memory", "stats", "--db", str(db), "--json"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["exists"] is True
    for layer in ("short", "working", "long"):
        assert body["layers"][layer]["count"] == 0
        assert body["layers"][layer]["bytes"] == 0
        assert body["layers"][layer]["pinned_count"] == 0
        assert body["layers"][layer]["oldest_ts"] is None
        assert body["layers"][layer]["newest_ts"] is None
