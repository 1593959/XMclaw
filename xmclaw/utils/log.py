"""Structured logging with rotation."""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from xmclaw.utils.paths import get_logs_dir

# Rotation defaults. Override with env vars for local debugging if needed.
_MAX_BYTES = int(os.environ.get("XMC_LOG_MAX_BYTES", 5 * 1024 * 1024))  # 5 MiB
_BACKUP_COUNT = int(os.environ.get("XMC_LOG_BACKUPS", 3))


def setup_logging():
    log_dir = get_logs_dir()
    log_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        handlers=[
            RotatingFileHandler(
                log_dir / "xmclaw.log",
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()


def rotate_if_large(
    log_file: Path,
    max_bytes: int = _MAX_BYTES,
    backups: int = _BACKUP_COUNT,
) -> None:
    """Rotate a plain (non-logging) log file before it's reopened.

    Used for files that are written via subprocess stdout redirection rather
    than the Python logging module — RotatingFileHandler doesn't apply there,
    so we do a one-shot rotation at each daemon start.

    Produces `<log_file>`, `<log_file>.1`, ..., `<log_file>.<backups>`, with
    `.1` being the most recent rotation. Oldest backup is dropped. Safe to
    call when the file does not yet exist.
    """
    try:
        if not log_file.exists() or log_file.stat().st_size < max_bytes:
            return

        # Drop the oldest, then shift each backup up by one.
        oldest = log_file.with_suffix(log_file.suffix + f".{backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(backups - 1, 0, -1):
            src = log_file.with_suffix(log_file.suffix + f".{i}")
            dst = log_file.with_suffix(log_file.suffix + f".{i + 1}")
            if src.exists():
                src.rename(dst)

        # Current file becomes .1
        log_file.rename(log_file.with_suffix(log_file.suffix + ".1"))
    except OSError:
        # Rotation is best-effort; never block daemon startup on log cleanup.
        pass


logger = setup_logging()
