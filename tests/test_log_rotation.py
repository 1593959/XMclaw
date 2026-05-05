"""Tests for xmclaw/utils/log.py — rotation, backup count, concurrency."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from xmclaw.utils.log import rotate_if_large


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_lines(path: Path, count: int, char: str = "x") -> None:
    """Append ``count`` lines of ~50 bytes each to ``path``."""
    chunk = char * 50
    with path.open("a", encoding="utf-8") as fh:
        for _ in range(count):
            fh.write(chunk + "\n")


# ---------------------------------------------------------------------------
# Test 1 — size threshold triggers rotation
# ---------------------------------------------------------------------------

class TestRotateIfLarge_SizeThreshold:
    """rotate_if_large renames the file and creates .1 when size >= max_bytes."""

    def test_rotate_creates_backup_and_clears_original(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        max_bytes = 512
        backups = 3

        # Write enough lines to push past the threshold (≈ 50 bytes/line).
        write_lines(log_file, 15)  # ~750 bytes > 512
        assert log_file.exists()
        assert log_file.stat().st_size >= max_bytes

        rotate_if_large(log_file, max_bytes=max_bytes, backups=backups)

        # .1 backup must exist and be non-empty.
        backup1 = log_file.with_suffix(log_file.suffix + ".1")
        assert backup1.exists(), "rotation failed: .1 backup not created"
        assert backup1.stat().st_size >= max_bytes, ".1 should contain the pre-rotation content"

        # Original file was renamed — no longer exists.
        assert not log_file.exists(), "original log file should be renamed away"

    def test_no_rotation_when_below_threshold(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        write_lines(log_file, 5)  # ~250 bytes, well below 512

        rotate_if_large(log_file, max_bytes=512, backups=3)

        assert log_file.exists()
        assert not (log_file.with_suffix(log_file.suffix + ".1")).exists()

    def test_no_rotation_when_file_does_not_exist(self, tmp_path: Path) -> None:
        log_file = tmp_path / "does-not-exist.log"
        rotate_if_large(log_file, max_bytes=512, backups=3)
        assert not log_file.exists()


# ---------------------------------------------------------------------------
# Test 2 — keep N old files (backup count enforcement)
# ---------------------------------------------------------------------------

class TestRotateIfLarge_BackupCount:
    """rotate_if_large drops the oldest backup when count would exceed limit."""

    def test_oldest_backup_pruned(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        max_bytes = 256
        backups = 2  # keep at most 2 backups (.1 and .2)

        # First rotation: creates .1
        write_lines(log_file, 8)  # ≈400 bytes > 256
        rotate_if_large(log_file, max_bytes=max_bytes, backups=backups)
        assert log_file.with_suffix(log_file.suffix + ".1").exists()
        assert not log_file.with_suffix(log_file.suffix + ".2").exists()

        # Second rotation: .1 → .2, fresh .1
        write_lines(log_file, 8)
        rotate_if_large(log_file, max_bytes=max_bytes, backups=backups)
        assert log_file.with_suffix(log_file.suffix + ".1").exists()
        assert log_file.with_suffix(log_file.suffix + ".2").exists()

        # Third rotation: .2 is oldest, must be dropped before .1 → .2
        write_lines(log_file, 8)
        rotate_if_large(log_file, max_bytes=max_bytes, backups=backups)

        # .3 must NOT exist (would exceed 2 backups)
        assert not log_file.with_suffix(log_file.suffix + ".3").exists(), ".3 backup leaked — oldest not pruned"
        # .1 and .2 must still exist
        assert log_file.with_suffix(log_file.suffix + ".1").exists()
        assert log_file.with_suffix(log_file.suffix + ".2").exists()

    def test_backups_ordered_correctly(self, tmp_path: Path) -> None:
        """Newer backups should have higher suffix numbers (more recent = lower)."""
        log_file = tmp_path / "app.log"
        max_bytes = 200
        backups = 3

        # Write, rotate, write more, rotate — three times.
        for i in range(3):
            write_lines(log_file, 6)
            rotate_if_large(log_file, max_bytes=max_bytes, backups=backups)
            time.sleep(0.01)  # ensure mtime differs on Windows

        # .3 is the oldest, .1 is the most recent.
        assert log_file.with_suffix(log_file.suffix + ".1").exists()
        assert log_file.with_suffix(log_file.suffix + ".2").exists()
        assert log_file.with_suffix(log_file.suffix + ".3").exists()

        # .1 should be newest by mtime.
        mtimes = {n: log_file.with_suffix(log_file.suffix + f".{n}").stat().st_mtime for n in (1, 2, 3)}
        assert mtimes[1] >= mtimes[2] >= mtimes[3], "backup order violated"


# ---------------------------------------------------------------------------
# Test 3 — concurrent writes are safe (RotatingFileHandler)
# ---------------------------------------------------------------------------

class TestRotatingFileHandler_Concurrency:
    """RotatingFileHandler has internal locking for concurrent writes."""

    def test_concurrent_log_writes_produce_valid_backups(self, tmp_path: Path) -> None:
        """Multiple threads write to a RotatingFileHandler; rotation completes cleanly."""
        log_path = tmp_path / "concurrent.log"
        max_bytes = 400
        backup_count = 3
        errors: list[BaseException] = []

        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        logger = logging.getLogger("concurrent_test")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)

        def writer() -> None:
            try:
                for _ in range(40):
                    logger.info("line" * 60)  # ~240 bytes
                    time.sleep(0.001)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(writer) for _ in range(8)]
                for f in futures:
                    f.result()

            handler.flush()

            assert not errors, f"concurrent writes raised: {errors}"
            # At least one backup must exist (rotation happened).
            backup1 = log_path.with_suffix(log_path.suffix + ".1")
            assert backup1.exists(), "handler produced no .1 backup despite exceeding threshold"
        finally:
            handler.close()
            logger.removeHandler(handler)

    def test_concurrent_rotation_no_file_corruption(self, tmp_path: Path) -> None:
        """Concurrent writes don't corrupt the log file."""
        log_path = tmp_path / "no-corrupt.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=300,
            backupCount=2,
            encoding="utf-8",
        )
        logger = logging.getLogger("no_corrupt_test")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)

        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for i in range(50):
                    logger.info(f"message_{i:03d}" * 12)  # ~150 bytes
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(writer) for _ in range(6)]
                for f in futures:
                    f.result()

            handler.flush()

            assert not errors, f"writer threads raised: {errors}"
            # All files should be readable and valid UTF-8.
            for child in log_path.parent.iterdir():
                if child.name.startswith(log_path.name):
                    content = child.read_text(encoding="utf-8")
                    assert content, f"{child.name} is empty or corrupted"
        finally:
            handler.close()
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Utility — rotate_if_large best-effort behavior
# ---------------------------------------------------------------------------

class TestRotateIfLarge_EdgeCases:
    """rotate_if_large is best-effort; concurrent calls tolerated but not guaranteed correct."""

    def test_concurrent_rotation_no_crash(self, tmp_path: Path) -> None:
        """Multiple threads call rotate_if_large; no exceptions raised (best-effort)."""
        log_file = tmp_path / "app.log"
        max_bytes = 300
        backups = 2
        error_occurred: list[BaseException] = []

        def rotate() -> None:
            try:
                rotate_if_large(log_file, max_bytes=max_bytes, backups=backups)
            except BaseException as exc:  # noqa: BLE001
                error_occurred.append(exc)

        # Populate file past threshold.
        write_lines(log_file, 12)  # ~600 bytes

        # 10 threads racing.
        threads = [threading.Thread(target=rotate) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not error_occurred, f"rotation raised in concurrent path: {error_occurred}"


class TestRotatingFileHandler_Integration:
    """Sanity-check the stdlib handler used internally (backupCount wiring)."""

    def test_handler_respects_backup_count(self, tmp_path: Path) -> None:
        log_path = tmp_path / "handler.log"
        max_bytes = 300
        backup_count = 2

        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        logger = logging.getLogger("test_handler")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)

        try:
            # Write past threshold multiple times.
            for _ in range(5):
                logger.info("line" * 60)  # ~200 bytes each, 2 writes ≈ 400 > 300

            # Wait for handler to flush.
            handler.flush()

            assert (log_path.with_suffix(log_path.suffix + ".1")).exists()
            assert (log_path.with_suffix(log_path.suffix + ".2")).exists()
            assert not (log_path.with_suffix(log_path.suffix + ".3")).exists(), "RotatingFileHandler let .3 through"
        finally:
            handler.close()
            logger.removeHandler(handler)
