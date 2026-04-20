"""Smoke tests for rotate_if_large."""
from pathlib import Path

from xmclaw.utils.log import rotate_if_large


def test_noop_when_file_missing(tmp_path: Path):
    log = tmp_path / "missing.log"
    rotate_if_large(log, max_bytes=10, backups=3)
    assert not log.exists()
    assert not (tmp_path / "missing.log.1").exists()


def test_noop_when_under_threshold(tmp_path: Path):
    log = tmp_path / "small.log"
    log.write_bytes(b"a" * 5)
    rotate_if_large(log, max_bytes=100, backups=3)
    assert log.exists()
    assert log.read_bytes() == b"a" * 5
    assert not (tmp_path / "small.log.1").exists()


def test_rotates_when_oversized(tmp_path: Path):
    log = tmp_path / "big.log"
    log.write_bytes(b"b" * 200)
    rotate_if_large(log, max_bytes=100, backups=3)
    assert not log.exists()
    assert (tmp_path / "big.log.1").exists()
    assert (tmp_path / "big.log.1").read_bytes() == b"b" * 200


def test_shifts_existing_backups(tmp_path: Path):
    log = tmp_path / "chain.log"
    log.write_bytes(b"new" * 100)  # oversized
    (tmp_path / "chain.log.1").write_bytes(b"one")
    (tmp_path / "chain.log.2").write_bytes(b"two")
    rotate_if_large(log, max_bytes=50, backups=3)
    assert (tmp_path / "chain.log.1").read_bytes() == b"new" * 100
    assert (tmp_path / "chain.log.2").read_bytes() == b"one"
    assert (tmp_path / "chain.log.3").read_bytes() == b"two"
    assert not log.exists()


def test_drops_oldest_beyond_backup_count(tmp_path: Path):
    log = tmp_path / "cap.log"
    log.write_bytes(b"x" * 200)
    (tmp_path / "cap.log.1").write_bytes(b"one")
    (tmp_path / "cap.log.2").write_bytes(b"two")
    (tmp_path / "cap.log.3").write_bytes(b"three")  # will be dropped
    rotate_if_large(log, max_bytes=100, backups=3)
    assert (tmp_path / "cap.log.1").read_bytes() == b"x" * 200
    assert (tmp_path / "cap.log.2").read_bytes() == b"one"
    assert (tmp_path / "cap.log.3").read_bytes() == b"two"
    assert not (tmp_path / "cap.log.4").exists()
