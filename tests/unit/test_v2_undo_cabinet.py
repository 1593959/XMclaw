"""Unit tests for UndoCabinet (Sprint 0 Track B)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.security.undo_cabinet import UndoCabinet, UndoRecord


@pytest.fixture
def cab(tmp_path):
    return UndoCabinet(root=tmp_path, window_s=300.0)


def _call(name, **args):
    return ToolCall(
        id=f"t-{name}",
        name=name,
        args=args,
        provenance="synthetic",
    )


# ── UndoCabinet unit tests ──────────────────────────────────────────


def test_record_for_existing_file_creates_backup(cab, tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    action_id = cab.record_file_mutation(
        path=target, action="file_write",
    )
    records = cab.recent()
    assert len(records) == 1
    assert records[0].id == action_id
    assert records[0].pre_existed is True
    assert records[0].backup_path
    # Backup file must contain original bytes
    backup_bytes = Path(records[0].backup_path).read_bytes()
    assert backup_bytes == b"ORIGINAL"


def test_record_for_new_file_marks_pre_existed_false(cab, tmp_path):
    target = tmp_path / "newfile.txt"
    # File doesn't exist yet
    action_id = cab.record_file_mutation(
        path=target, action="file_write",
    )
    records = cab.recent()
    assert len(records) == 1
    assert records[0].id == action_id
    assert records[0].pre_existed is False
    assert records[0].backup_path is None


def test_undo_restores_pre_existing_file(cab, tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("BEFORE", encoding="utf-8")
    action_id = cab.record_file_mutation(path=target, action="file_write")
    # Simulate the mutation
    target.write_text("AFTER", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "AFTER"
    result = cab.undo(action_id)
    assert result["applied"] is True
    assert result["reverse_kind"] == "restored_from_backup"
    assert target.read_text(encoding="utf-8") == "BEFORE"


def test_undo_deletes_newly_created_file(cab, tmp_path):
    target = tmp_path / "created.txt"
    action_id = cab.record_file_mutation(path=target, action="file_write")
    # Simulate creation
    target.write_text("NEW CONTENT", encoding="utf-8")
    result = cab.undo(action_id)
    assert result["applied"] is True
    assert result["reverse_kind"] == "deleted_created_file"
    assert not target.exists()


def test_undo_idempotent(cab, tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("hi", encoding="utf-8")
    action_id = cab.record_file_mutation(path=target, action="file_write")
    target.write_text("ho", encoding="utf-8")
    assert cab.undo(action_id)["applied"] is True
    # Second undo of the same id is no-op
    second = cab.undo(action_id)
    assert second["applied"] is False
    assert "undone" in second["reason"]


def test_undo_unknown_id(cab):
    result = cab.undo("bogus-id-12345")
    assert result["applied"] is False
    assert "not found" in result["reason"]


def test_recent_returns_newest_first(cab, tmp_path):
    a = cab.record_file_mutation(path=tmp_path / "a", action="file_write")
    time.sleep(0.01)
    b = cab.record_file_mutation(path=tmp_path / "b", action="file_write")
    time.sleep(0.01)
    c = cab.record_file_mutation(path=tmp_path / "c", action="file_write")
    recs = cab.recent()
    assert [r.id for r in recs] == [c, b, a]


def test_recent_within_s_filters(cab, tmp_path):
    a = cab.record_file_mutation(path=tmp_path / "a", action="file_write")
    time.sleep(0.05)
    recs = cab.recent(within_s=0.001)
    # 'a' is older than 0.001s — should be filtered
    assert a not in [r.id for r in recs]
    recs2 = cab.recent(within_s=10)
    assert a in [r.id for r in recs2]


def test_undo_recent_batch_undoes_active_records(cab, tmp_path):
    p1 = tmp_path / "p1"
    p1.write_text("orig1", encoding="utf-8")
    p2 = tmp_path / "p2"  # new
    id1 = cab.record_file_mutation(path=p1, action="file_write")
    id2 = cab.record_file_mutation(path=p2, action="file_write")
    p1.write_text("mod1", encoding="utf-8")
    p2.write_text("new2", encoding="utf-8")
    results = cab.undo_recent(within_s=60)
    assert sum(1 for r in results if r["applied"]) == 2
    assert p1.read_text(encoding="utf-8") == "orig1"
    assert not p2.exists()


def test_undo_recent_action_filter(cab, tmp_path):
    p1 = tmp_path / "p1"
    p1.write_text("a", encoding="utf-8")
    p2 = tmp_path / "p2"
    p2.write_text("b", encoding="utf-8")
    cab.record_file_mutation(path=p1, action="file_write")
    cab.record_file_mutation(path=p2, action="file_delete")
    results = cab.undo_recent(within_s=60, action_filter="file_delete")
    assert len(results) == 1


def test_gc_expires_old_records(tmp_path):
    short = UndoCabinet(root=tmp_path, window_s=0.05)
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    short.record_file_mutation(path=target, action="file_write")
    time.sleep(0.1)
    short._gc()
    # After GC, the record is no longer active.
    recs = short.recent(status="active")
    assert len(recs) == 0
    # Check the underlying DB shows status="expired".
    import sqlite3
    conn = sqlite3.connect(short._db_path)
    row = conn.execute(
        "SELECT status, backup_path FROM actions LIMIT 1",
    ).fetchone()
    conn.close()
    assert row[0] == "expired"
    assert row[1] is None  # backup_path cleared on GC


# ── Integration with builtin_fs ─────────────────────────────────────


async def _build_tools_with_cabinet(tmp_path):
    cab = UndoCabinet(root=tmp_path / ".undo", window_s=300.0)
    tools = BuiltinTools(
        allowed_dirs=[tmp_path],
        undo_cabinet=cab,
    )
    return tools, cab


@pytest.mark.asyncio
async def test_file_write_records_undo(tmp_path):
    tools, cab = await _build_tools_with_cabinet(tmp_path)
    target = tmp_path / "report.md"
    result = await tools.invoke(
        _call("file_write", path=str(target), content="hello"),
    )
    assert result.ok is True
    assert "undo_id" in result.content
    records = cab.recent()
    assert len(records) == 1
    assert records[0].action == "file_write"


@pytest.mark.asyncio
async def test_undo_recent_via_tool_restores_file(tmp_path):
    tools, _cab = await _build_tools_with_cabinet(tmp_path)
    target = tmp_path / "doc.md"
    target.write_text("ORIGINAL", encoding="utf-8")
    await tools.invoke(
        _call("file_write", path=str(target), content="NEW"),
    )
    assert target.read_text(encoding="utf-8") == "NEW"
    r = await tools.invoke(_call("undo_recent", within_s=10))
    assert r.ok is True
    assert r.content["applied_count"] == 1
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


@pytest.mark.asyncio
async def test_file_delete_records_undo_and_undoes(tmp_path):
    tools, _cab = await _build_tools_with_cabinet(tmp_path)
    target = tmp_path / "to_delete.txt"
    target.write_text("DOOMED", encoding="utf-8")
    r = await tools.invoke(_call("file_delete", path=str(target)))
    assert r.ok is True
    assert not target.exists()
    # Undo restores
    undo = await tools.invoke(_call("undo_recent", within_s=10))
    assert undo.ok is True
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "DOOMED"


@pytest.mark.asyncio
async def test_apply_patch_records_undo(tmp_path):
    tools, _cab = await _build_tools_with_cabinet(tmp_path)
    target = tmp_path / "src.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    r = await tools.invoke(_call(
        "apply_patch", path=str(target),
        edits=[{"old_text": "a = 1", "new_text": "a = 99"}],
    ))
    assert r.ok is True
    assert "undo_id" in r.content
    assert "a = 99" in target.read_text(encoding="utf-8")
    # Undo
    undo = await tools.invoke(_call("undo_recent", within_s=10))
    assert undo.ok is True
    assert "a = 1" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_undo_list_tool_returns_records(tmp_path):
    tools, _cab = await _build_tools_with_cabinet(tmp_path)
    target = tmp_path / "x.txt"
    await tools.invoke(_call("file_write", path=str(target), content="a"))
    r = await tools.invoke(_call("undo_list", within_s=60))
    assert r.ok is True
    assert r.content["count"] == 1
    assert r.content["records"][0]["action"] == "file_write"


@pytest.mark.asyncio
async def test_undo_recent_by_action_id(tmp_path):
    tools, _cab = await _build_tools_with_cabinet(tmp_path)
    target = tmp_path / "y.txt"
    target.write_text("ORIG", encoding="utf-8")
    write_result = await tools.invoke(_call(
        "file_write", path=str(target), content="MOD",
    ))
    action_id = write_result.content["undo_id"]
    undo = await tools.invoke(
        _call("undo_recent", action_id=action_id),
    )
    assert undo.ok is True
    assert undo.content["applied"] is True
    assert target.read_text(encoding="utf-8") == "ORIG"


@pytest.mark.asyncio
async def test_undo_tools_not_listed_without_cabinet():
    tools = BuiltinTools(undo_cabinet=None)
    names = [s.name for s in tools.list_tools()]
    assert "undo_recent" not in names
    assert "undo_list" not in names


@pytest.mark.asyncio
async def test_undo_tools_listed_with_cabinet(tmp_path):
    tools, _cab = await _build_tools_with_cabinet(tmp_path)
    names = [s.name for s in tools.list_tools()]
    assert "undo_recent" in names
    assert "undo_list" in names
