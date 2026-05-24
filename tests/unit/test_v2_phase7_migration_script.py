"""Phase 7.B.2 — migration-script scan + classification tests.

The script's HTTP-side path (the actual POST loop) needs a running
daemon to test; that's covered manually in §7.B.3. Here we pin the
PURE-PYTHON parts: _scan_rows row classification + _make_backup
behaviour. Both are critical for migration safety + can be
covered with an in-memory sqlite stand-in.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_memory_db_to_v2 import (
    _make_backup,
    _scan_rows,
    BACKUP_SUFFIX,
)


def _make_db(tmp_path: Path, rows: list[dict]) -> Path:
    """Build a minimal memory.db-compatible sqlite file."""
    db = tmp_path / "memory.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE memory_items ("
            "  id TEXT PRIMARY KEY, layer TEXT, text TEXT, "
            "  metadata TEXT, ts REAL, evidence_count INTEGER, "
            "  confidence REAL"
            ")"
        )
        for r in rows:
            con.execute(
                "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"],
                    r.get("layer", "long"),
                    r["text"],
                    json.dumps(r.get("metadata", {})),
                    r.get("ts", 1715000000.0),
                    r.get("evidence_count", 1),
                    r.get("confidence", 0.7),
                ),
            )
        con.commit()
    finally:
        con.close()
    return db


def test_scan_classifies_lessons(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [
        {
            "id": "l1", "text": "always run tests before push",
            "metadata": {"kind": "lesson", "bucket": "workflow"},
        },
    ])
    lessons, manuals, bullets, skipped = _scan_rows(db)
    assert len(lessons) == 1
    assert lessons[0]["bucket"] == "workflow"
    assert lessons[0]["text"] == "always run tests before push"
    assert manuals == []
    assert bullets == []
    assert dict(skipped) == {}


def test_scan_classifies_persona_manual(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [
        {
            "id": "pm1", "text": "## My identity\nAlice, ML engineer",
            "metadata": {"kind": "persona_manual", "file": "IDENTITY.md"},
        },
    ])
    _, manuals, _, _ = _scan_rows(db)
    assert len(manuals) == 1
    assert manuals[0]["basename"] == "IDENTITY.md"


def test_scan_classifies_persona_bullet(tmp_path: Path) -> None:
    """Phase 7.B.2 added persona_bullet coverage."""
    db = _make_db(tmp_path, [
        {
            "id": "b1", "text": "- prefers concise replies",
            "metadata": {"kind": "persona_bullet", "path": "MEMORY.md"},
        },
    ])
    _, _, bullets, _ = _scan_rows(db)
    assert len(bullets) == 1
    assert bullets[0]["source_path"] == "MEMORY.md"


def test_scan_skips_file_chunk_and_code_chunk(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [
        {
            "id": "fc1", "text": "chunk-1 contents",
            "metadata": {"kind": "file_chunk"},
        },
        {
            "id": "cc1", "text": "def foo(): pass",
            "metadata": {"kind": "code_chunk"},
        },
    ])
    lessons, manuals, bullets, skipped = _scan_rows(db)
    assert lessons == manuals == bullets == []
    assert skipped["file_chunk"] == 1
    assert skipped["code_chunk"] == 1


def test_scan_skips_lesson_without_bucket(tmp_path: Path) -> None:
    """A lesson row missing the bucket field can't be routed; skip
    explicitly rather than guess."""
    db = _make_db(tmp_path, [
        {
            "id": "l1", "text": "x",
            "metadata": {"kind": "lesson"},  # no bucket
        },
    ])
    lessons, _, _, skipped = _scan_rows(db)
    assert lessons == []
    assert skipped["_lesson_no_bucket"] == 1


def test_scan_skips_malformed_rows(tmp_path: Path) -> None:
    """Empty text or unparseable metadata → counted as malformed."""
    db = _make_db(tmp_path, [
        {"id": "x", "text": "", "metadata": {"kind": "lesson"}},
        {"id": "y", "text": "valid",
         "metadata": "not-a-dict"},  # serialized as JSON string "not-a-dict"
    ])
    lessons, manuals, bullets, skipped = _scan_rows(db)
    assert lessons == manuals == bullets == []
    assert skipped["_malformed"] >= 1


def test_scan_records_unknown_kinds_under_their_name(tmp_path: Path) -> None:
    """Unknown / unsupported kinds appear in the skipped counter
    keyed by their string so operator can see what's being ignored."""
    db = _make_db(tmp_path, [
        {"id": "x", "text": "summary text",
         "metadata": {"kind": "session_summary"}},
    ])
    _, _, _, skipped = _scan_rows(db)
    assert skipped["session_summary"] == 1


def test_backup_creates_sibling_file(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [
        {"id": "x", "text": "y", "metadata": {"kind": "lesson", "bucket": "workflow"}},
    ])
    backup = _make_backup(db)
    assert backup.exists()
    assert backup.name == "memory.db" + BACKUP_SUFFIX
    # Sizes should match (it's a copy).
    assert backup.stat().st_size == db.stat().st_size


def test_backup_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """If backup already exists, don't overwrite — operator may have
    a known-good rollback target."""
    db = _make_db(tmp_path, [
        {"id": "x", "text": "y", "metadata": {"kind": "lesson", "bucket": "workflow"}},
    ])
    backup = _make_backup(db)
    # Append something to the original so sizes differ.
    with db.open("ab") as fh:
        fh.write(b"\x00" * 100)
    backup_size_before = backup.stat().st_size
    _make_backup(db)
    capture = capsys.readouterr()
    assert "skipping copy" in capture.out
    assert backup.stat().st_size == backup_size_before  # untouched
