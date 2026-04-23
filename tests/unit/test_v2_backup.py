"""Epic #20 — backup create / list / restore unit tests.

Round-trip is the spine: make a synthetic workspace, create a backup,
mutate the workspace, restore, assert original state. Everything else
(checksum mismatch, tar-slip, exclude rules, schema version gate) are
individual guards around that round-trip.

Tests use ``tmp_path`` exclusively — nothing touches ``~/.xmclaw/``.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xmclaw.backup import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    create_backup,
    list_backups,
    restore_backup,
)
from xmclaw.backup.create import DEFAULT_EXCLUDED, BackupError, _is_excluded
from xmclaw.backup.restore import RestoreError
from xmclaw.backup.store import ARCHIVE_NAME
from xmclaw.cli.main import app


# ── helpers ─────────────────────────────────────────────────────────────


def _make_workspace(root: Path) -> None:
    """Lay out a minimal synthetic ``~/.xmclaw/`` workspace."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "v2").mkdir()
    (root / "v2" / "events.db").write_text("fake-events-db\n", encoding="utf-8")
    (root / "v2" / "memory.db").write_text("fake-memory-db\n", encoding="utf-8")
    (root / "v2" / "pairing_token.txt").write_text("tok", encoding="utf-8")
    (root / "v2" / "daemon.pid").write_text("42", encoding="utf-8")
    (root / "v2" / "daemon.meta").write_text("{}", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "xmclaw.log").write_text("spam" * 100, encoding="utf-8")
    (root / "v2" / "__pycache__").mkdir()
    (root / "v2" / "__pycache__" / "x.cpython.pyc").write_bytes(b"\x00\x00")


# ── exclude-pattern unit ────────────────────────────────────────────────


def test_is_excluded_matches_logs_dir() -> None:
    assert _is_excluded("logs", DEFAULT_EXCLUDED)
    assert _is_excluded("logs/xmclaw.log", DEFAULT_EXCLUDED)


def test_is_excluded_matches_pycache_anywhere() -> None:
    assert _is_excluded("v2/__pycache__", DEFAULT_EXCLUDED)
    assert _is_excluded("v2/__pycache__/x.pyc", DEFAULT_EXCLUDED)


def test_is_excluded_matches_pid_and_meta() -> None:
    assert _is_excluded("daemon.pid", DEFAULT_EXCLUDED)
    assert _is_excluded("daemon.meta", DEFAULT_EXCLUDED)
    assert _is_excluded("v2/daemon.pid", DEFAULT_EXCLUDED)


def test_is_excluded_rejects_regular_files() -> None:
    assert not _is_excluded("v2/events.db", DEFAULT_EXCLUDED)
    assert not _is_excluded("v2/memory.db", DEFAULT_EXCLUDED)
    assert not _is_excluded("v2/pairing_token.txt", DEFAULT_EXCLUDED)


def test_is_excluded_handles_windows_backslashes() -> None:
    """Glob matching must be posix-normalized so one pattern list works
    on both platforms."""
    assert _is_excluded("v2\\daemon.pid", DEFAULT_EXCLUDED)
    assert _is_excluded("logs\\xmclaw.log", DEFAULT_EXCLUDED)


# ── create_backup ───────────────────────────────────────────────────────


def test_create_backup_writes_archive_and_manifest(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    manifest = create_backup(ws, "first", backups_dir=dest)
    assert (dest / "first" / ARCHIVE_NAME).is_file()
    assert (dest / "first" / MANIFEST_NAME).is_file()
    assert manifest.name == "first"
    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.archive_bytes > 0
    assert len(manifest.archive_sha256) == 64  # hex SHA-256


def test_create_backup_excludes_logs_and_pid(tmp_path: Path) -> None:
    """logs/, daemon.pid, daemon.meta, __pycache__ must not land in the
    tarball — the workspace is reproducible without them and they bloat
    or leak machine-specific state."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "b1", backups_dir=dest)
    with tarfile.open(dest / "b1" / ARCHIVE_NAME, "r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
    assert "v2/events.db" in names
    assert "v2/memory.db" in names
    assert "logs" not in names
    assert "logs/xmclaw.log" not in names
    assert "v2/daemon.pid" not in names
    assert "v2/daemon.meta" not in names
    assert not any("__pycache__" in n for n in names)


def test_create_backup_checksum_matches_archive(tmp_path: Path) -> None:
    """Manifest checksum must actually be the tarball's sha256, not a
    stale copy. Restore's integrity guard relies on this."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    manifest = create_backup(ws, "cksum", backups_dir=dest)
    archive = dest / "cksum" / ARCHIVE_NAME
    hasher = hashlib.sha256()
    hasher.update(archive.read_bytes())
    assert hasher.hexdigest() == manifest.archive_sha256


def test_create_backup_refuses_to_clobber(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "dup", backups_dir=dest)
    with pytest.raises(BackupError, match="already exists"):
        create_backup(ws, "dup", backups_dir=dest)


def test_create_backup_overwrites_when_asked(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "dup", backups_dir=dest)
    # Make the content different and overwrite — checksum should change.
    (ws / "v2" / "new.txt").write_text("changed", encoding="utf-8")
    m2 = create_backup(ws, "dup", backups_dir=dest, overwrite=True)
    with tarfile.open(dest / "dup" / ARCHIVE_NAME, "r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
    assert "v2/new.txt" in names
    assert m2.entries > 0


def test_create_backup_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="does not exist"):
        create_backup(tmp_path / "nope", "x", backups_dir=tmp_path / "b")


def test_create_backup_rejects_path_separators_in_name(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    with pytest.raises(BackupError, match="invalid backup name"):
        create_backup(ws, "a/b", backups_dir=tmp_path / "backups")
    with pytest.raises(BackupError, match="invalid backup name"):
        create_backup(ws, "..", backups_dir=tmp_path / "backups")


def test_create_backup_cleans_up_stale_staging(tmp_path: Path) -> None:
    """If a previous run crashed mid-create, its ``<name>.tmp`` dir may
    still be there. The next run should take over cleanly rather than
    error on the leftover."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    dest.mkdir()
    stale = dest / "rerun.tmp"
    stale.mkdir()
    (stale / "leftover.txt").write_text("junk", encoding="utf-8")
    manifest = create_backup(ws, "rerun", backups_dir=dest)
    assert not stale.exists()
    assert manifest.name == "rerun"


# ── Manifest schema ─────────────────────────────────────────────────────


def test_manifest_round_trip(tmp_path: Path) -> None:
    m = Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        name="a", created_ts=123.0, xmclaw_version="0.0.0",
        archive_sha256="0" * 64, archive_bytes=1,
        source_dir=str(tmp_path), excluded=("logs",), entries=5,
    )
    p = tmp_path / "m.json"
    m.write(p)
    loaded = Manifest.load(p)
    assert loaded == m


def test_manifest_read_ignores_extra_fields(tmp_path: Path) -> None:
    """A newer xmclaw may have added fields. Older code should ignore
    them, not crash."""
    p = tmp_path / "m.json"
    payload = {
        "schema_version": 1, "name": "x", "created_ts": 0.0,
        "xmclaw_version": "z", "archive_sha256": "0" * 64,
        "archive_bytes": 0, "source_dir": "/", "excluded": [],
        "entries": 0,
        "future_field_from_v2": "dont-crash",
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    m = Manifest.load(p)
    assert m.name == "x"


# ── list_backups ────────────────────────────────────────────────────────


def test_list_backups_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "never-existed") == []


def test_list_backups_enumerates_well_formed(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    import time

    create_backup(ws, "a", backups_dir=dest)
    # Ensure the second backup's created_ts is strictly later so sort
    # order is deterministic even on FAT-granularity filesystems.
    time.sleep(0.01)
    create_backup(ws, "b", backups_dir=dest)
    entries = list_backups(dest)
    assert [e.name for e in entries] == ["a", "b"]
    # Sorted by created_ts asc.
    assert entries[0].manifest.created_ts <= entries[1].manifest.created_ts


def test_list_backups_skips_malformed(tmp_path: Path) -> None:
    """A directory missing its manifest or with corrupt json shouldn't
    blow up the listing."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "good", backups_dir=dest)
    # 1) missing-manifest dir
    (dest / "missing-manifest").mkdir()
    (dest / "missing-manifest" / ARCHIVE_NAME).write_bytes(b"not a tar")
    # 2) corrupt-manifest dir
    (dest / "corrupt").mkdir()
    (dest / "corrupt" / ARCHIVE_NAME).write_bytes(b"not a tar")
    (dest / "corrupt" / MANIFEST_NAME).write_text("{ this is not json",
                                                  encoding="utf-8")
    # 3) stray file at the root (not a dir)
    (dest / "stray.txt").write_text("x", encoding="utf-8")
    entries = list_backups(dest)
    assert [e.name for e in entries] == ["good"]


# ── restore round-trip ──────────────────────────────────────────────────


def test_restore_round_trips_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "snapshot", backups_dir=dest)

    # Corrupt the workspace.
    (ws / "v2" / "events.db").write_text("CORRUPTED", encoding="utf-8")
    (ws / "v2" / "memory.db").unlink()

    restore_backup("snapshot", ws, backups_dir=dest)

    assert (ws / "v2" / "events.db").read_text(encoding="utf-8") == (
        "fake-events-db\n"
    )
    assert (ws / "v2" / "memory.db").read_text(encoding="utf-8") == (
        "fake-memory-db\n"
    )


def test_restore_keeps_previous_tree(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "snap", backups_dir=dest)
    (ws / "v2" / "events.db").write_text("mutated", encoding="utf-8")
    restore_backup("snap", ws, backups_dir=dest, keep_previous=True)
    # Exactly one sibling named xmclaw.prev-* should exist.
    siblings = [p for p in tmp_path.iterdir() if p.name.startswith("xmclaw.prev-")]
    assert len(siblings) == 1
    assert (siblings[0] / "v2" / "events.db").read_text(encoding="utf-8") == (
        "mutated"
    )


def test_restore_checksum_mismatch_raises(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "tamper", backups_dir=dest)
    # Flip a byte in the tarball — sha256 must reject.
    archive = dest / "tamper" / ARCHIVE_NAME
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF
    archive.write_bytes(bytes(data))
    with pytest.raises(RestoreError, match="checksum mismatch"):
        restore_backup("tamper", tmp_path / "fresh", backups_dir=dest)


def test_restore_rejects_newer_schema(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "future", backups_dir=dest)
    manifest_path = dest / "future" / MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = MANIFEST_SCHEMA_VERSION + 7
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RestoreError, match="newer than supported"):
        restore_backup("future", tmp_path / "fresh", backups_dir=dest)


def test_restore_rejects_missing_backup(tmp_path: Path) -> None:
    with pytest.raises(RestoreError, match="backup not found"):
        restore_backup("nope", tmp_path / "t", backups_dir=tmp_path / "b")


def test_restore_rejects_tar_slip(tmp_path: Path) -> None:
    """A hand-crafted archive with ``../outside`` members must be
    refused — we don't trust the backup dir to contain only our files."""
    dest = tmp_path / "backups" / "evil"
    dest.mkdir(parents=True)

    # Build a gzipped tar with an escaping member.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive_bytes = buf.getvalue()
    (dest / ARCHIVE_NAME).write_bytes(archive_bytes)

    # Manifest must match the tampered archive's checksum or we'd fail
    # on integrity before tar-slip — we want to verify tar-slip itself.
    h = hashlib.sha256(archive_bytes).hexdigest()
    Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        name="evil", created_ts=0.0, xmclaw_version="t",
        archive_sha256=h, archive_bytes=len(archive_bytes),
        source_dir="/", excluded=(), entries=1,
    ).write(dest / MANIFEST_NAME)

    with pytest.raises(RestoreError, match="escapes target"):
        restore_backup("evil", tmp_path / "target", backups_dir=tmp_path / "backups")


def test_restore_leaves_no_staging_on_failure(tmp_path: Path) -> None:
    """A failed restore mid-way must clean up its ``.restore-staging``
    so the next attempt doesn't collide."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "tamper", backups_dir=dest)
    archive = dest / "tamper" / ARCHIVE_NAME
    data = bytearray(archive.read_bytes())
    data[-5] ^= 0xFF
    archive.write_bytes(bytes(data))
    target = tmp_path / "fresh"
    with pytest.raises(RestoreError):
        restore_backup("tamper", target, backups_dir=dest)
    # checksum fails BEFORE staging dir creation so nothing to clean.
    assert not target.with_name(target.name + ".restore-staging").exists()


# ── CLI ─────────────────────────────────────────────────────────────────


def test_cli_backup_create_list_restore_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end via ``xmclaw backup create | list | restore``."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    runner = CliRunner()

    r = runner.invoke(
        app,
        ["backup", "create", "cli-test",
         "--source", str(ws), "--dest", str(dest)],
    )
    assert r.exit_code == 0, r.output
    assert "cli-test" in r.output
    assert "sha256=" in r.output

    r = runner.invoke(app, ["backup", "list", "--dest", str(dest)])
    assert r.exit_code == 0, r.output
    assert "cli-test" in r.output

    (ws / "v2" / "events.db").write_text("BROKEN", encoding="utf-8")

    r = runner.invoke(
        app,
        ["backup", "restore", "cli-test",
         "--target", str(ws), "--dest", str(dest)],
    )
    assert r.exit_code == 0, r.output
    assert "restored" in r.output
    assert (ws / "v2" / "events.db").read_text(encoding="utf-8") == (
        "fake-events-db\n"
    )


def test_cli_backup_list_empty_says_so(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(
        app, ["backup", "list", "--dest", str(tmp_path / "no-backups")],
    )
    assert r.exit_code == 0, r.output
    assert "no backups" in r.output.lower()


def test_cli_backup_create_clashing_name_exits_nonzero(tmp_path: Path) -> None:
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "create", "dup", "--source", str(ws), "--dest", str(dest)],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        ["backup", "create", "dup", "--source", str(ws), "--dest", str(dest)],
    )
    assert r.exit_code == 1, r.output
    assert "error" in r.output


def test_cli_backup_restore_missing_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "restore", "ghost",
         "--target", str(tmp_path / "t"),
         "--dest", str(tmp_path / "backups")],
    )
    assert r.exit_code == 1, r.output
    assert "error" in r.output


def test_archive_is_valid_gzip(tmp_path: Path) -> None:
    """Sanity: the archive file should actually be gzip — catches a
    future refactor that might silently switch to plain tar."""
    ws = tmp_path / "xmclaw"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "gz", backups_dir=dest)
    with gzip.open(dest / "gz" / ARCHIVE_NAME, "rb") as fh:
        head = fh.read(512)
    assert head  # gzip decoded cleanly


def test_env_override_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``XMC_BACKUPS_DIR`` moves the backup root without code changes —
    used by containers and tests."""
    monkeypatch.setenv("XMC_BACKUPS_DIR", str(tmp_path / "custom"))
    from xmclaw.backup.store import default_backups_dir

    assert default_backups_dir() == tmp_path / "custom"


# ── delete + prune ──────────────────────────────────────────────────────


def test_delete_backup_removes_directory(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "b1", backups_dir=dest)
    from xmclaw.backup import delete_backup

    path = delete_backup("b1", backups_dir=dest)
    assert not path.exists()
    assert dest.exists()  # root not removed


def test_delete_backup_raises_when_missing(tmp_path: Path) -> None:
    from xmclaw.backup import BackupNotFoundError, delete_backup

    with pytest.raises(BackupNotFoundError):
        delete_backup("ghost", backups_dir=tmp_path / "empty")


def test_delete_backup_rejects_path_separators(tmp_path: Path) -> None:
    from xmclaw.backup import delete_backup

    with pytest.raises(ValueError):
        delete_backup("../etc/passwd", backups_dir=tmp_path / "backups")
    with pytest.raises(ValueError):
        delete_backup("", backups_dir=tmp_path / "backups")


def test_prune_keeps_newest_and_drops_older(tmp_path: Path) -> None:
    """prune(keep=2) on 5 backups drops 3 oldest."""
    import time as _time

    from xmclaw.backup import prune_backups
    from xmclaw.backup.manifest import (
        MANIFEST_NAME,
        MANIFEST_SCHEMA_VERSION,
        Manifest,
    )

    dest = tmp_path / "backups"
    dest.mkdir()
    for i, age in enumerate([500, 400, 300, 200, 100]):
        bdir = dest / f"b{i}"
        bdir.mkdir()
        (bdir / ARCHIVE_NAME).write_bytes(b"x")
        Manifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            name=f"b{i}",
            created_ts=_time.time() - age,
            xmclaw_version="0.0.0",
            archive_sha256="0" * 64,
            archive_bytes=1,
            source_dir=str(tmp_path),
            excluded=(),
            entries=0,
        ).write(bdir / MANIFEST_NAME)
    removed = prune_backups(backups_dir=dest, keep=2)
    assert removed == ["b0", "b1", "b2"]  # oldest three, in order
    assert sorted(p.name for p in dest.iterdir()) == ["b3", "b4"]


def test_prune_noop_when_under_keep(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "only", backups_dir=dest)
    from xmclaw.backup import prune_backups

    assert prune_backups(backups_dir=dest, keep=5) == []
    assert (dest / "only").is_dir()


def test_prune_rejects_negative_keep(tmp_path: Path) -> None:
    from xmclaw.backup import prune_backups

    with pytest.raises(ValueError):
        prune_backups(backups_dir=tmp_path / "x", keep=-1)


def test_cli_backup_delete_with_yes_removes_backup(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "doomed", backups_dir=dest)
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "delete", "doomed", "--dest", str(dest), "--yes"],
    )
    assert r.exit_code == 0, r.stdout
    assert not (dest / "doomed").exists()


def test_cli_backup_delete_missing_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "delete", "ghost", "--dest", str(tmp_path / "b"), "--yes"],
    )
    assert r.exit_code == 1


def test_cli_backup_prune_keep_respected(tmp_path: Path) -> None:
    import time as _time

    from xmclaw.backup.manifest import (
        MANIFEST_NAME,
        MANIFEST_SCHEMA_VERSION,
        Manifest,
    )

    dest = tmp_path / "backups"
    dest.mkdir()
    for i, age in enumerate([300, 200, 100]):
        bdir = dest / f"b{i}"
        bdir.mkdir()
        (bdir / ARCHIVE_NAME).write_bytes(b"x")
        Manifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            name=f"b{i}",
            created_ts=_time.time() - age,
            xmclaw_version="0.0.0",
            archive_sha256="0" * 64,
            archive_bytes=1,
            source_dir=str(tmp_path),
            excluded=(),
            entries=0,
        ).write(bdir / MANIFEST_NAME)
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "prune", "--keep", "1", "--dest", str(dest), "--yes"],
    )
    assert r.exit_code == 0, r.stdout
    assert sorted(p.name for p in dest.iterdir()) == ["b2"]
    assert "removed 2" in r.stdout


def test_cli_backup_prune_noop_says_nothing_to_prune(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "only", backups_dir=dest)
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "prune", "--keep", "5", "--dest", str(dest)],
    )
    assert r.exit_code == 0, r.stdout
    assert "nothing to prune" in r.stdout


# ── verify ──────────────────────────────────────────────────────────────


def test_verify_backup_returns_manifest_on_clean_archive(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    created = create_backup(ws, "clean", backups_dir=dest)
    from xmclaw.backup import verify_backup

    got = verify_backup("clean", backups_dir=dest)
    assert got.archive_sha256 == created.archive_sha256
    assert got.entries == created.entries


def test_verify_backup_detects_bit_flip(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "tocorrupt", backups_dir=dest)
    archive = dest / "tocorrupt" / ARCHIVE_NAME
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0x01
    archive.write_bytes(bytes(data))
    from xmclaw.backup import verify_backup

    with pytest.raises(RestoreError, match="checksum mismatch"):
        verify_backup("tocorrupt", backups_dir=dest)


def test_verify_backup_missing_raises(tmp_path: Path) -> None:
    from xmclaw.backup import verify_backup

    with pytest.raises(RestoreError, match="not found"):
        verify_backup("ghost", backups_dir=tmp_path / "nowhere")


def test_verify_backup_missing_archive_raises(tmp_path: Path) -> None:
    """Manifest present but archive gone should surface as RestoreError."""
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "half", backups_dir=dest)
    (dest / "half" / ARCHIVE_NAME).unlink()
    from xmclaw.backup import verify_backup

    with pytest.raises(RestoreError, match="archive missing"):
        verify_backup("half", backups_dir=dest)


def test_verify_backup_newer_schema_raises(tmp_path: Path) -> None:
    import time as _time

    from xmclaw.backup import verify_backup

    dest = tmp_path / "backups"
    bdir = dest / "futuristic"
    bdir.mkdir(parents=True)
    (bdir / ARCHIVE_NAME).write_bytes(b"x")
    # Hand-build a manifest with schema_version bumped into the future.
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION + 1,
        "name": "futuristic",
        "created_ts": _time.time(),
        "xmclaw_version": "99.0.0",
        "archive_sha256": hashlib.sha256(b"x").hexdigest(),
        "archive_bytes": 1,
        "source_dir": str(tmp_path),
        "excluded": [],
        "entries": 0,
    }
    (bdir / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RestoreError, match="schema"):
        verify_backup("futuristic", backups_dir=dest)


def test_cli_backup_verify_happy_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "ok1", backups_dir=dest)
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "verify", "ok1", "--dest", str(dest)],
    )
    assert r.exit_code == 0, r.stdout
    assert "verified" in r.stdout


def test_cli_backup_verify_corrupted_exits_nonzero(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_workspace(ws)
    dest = tmp_path / "backups"
    create_backup(ws, "bad", backups_dir=dest)
    archive = dest / "bad" / ARCHIVE_NAME
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0x01
    archive.write_bytes(bytes(data))
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["backup", "verify", "bad", "--dest", str(dest)],
    )
    assert r.exit_code == 1, r.stdout
