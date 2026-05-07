"""Structured logging with rotation, context vars, and secret scrubbing.

Public entry points:

* :func:`setup_logging` — wires stdlib ``logging`` + ``structlog`` to write
  JSON lines to ``~/.xmclaw/logs/xmclaw.log`` with size-based rotation.
  Idempotent: safe to call multiple times (later calls are no-ops). The
  first call is what actually configures handlers.
* :func:`get_logger` — return a bound ``structlog`` logger. Does **not**
  trigger setup, so modules can import it freely without creating log
  files as an import side effect.
* :func:`bind_log_context` / :func:`clear_log_context` — thin wrappers
  around ``structlog.contextvars`` so turn/session scope can be pinned
  once and every downstream log line automatically carries it.
* :func:`rotate_if_large` — one-shot rotation for plain (non-``logging``)
  files written via subprocess stdout redirection; ``RotatingFileHandler``
  doesn't apply to those.

**Why no module-scope ``setup_logging()``**: the ``xmclaw/utils/AGENTS.md``
contract says utils must be pure on import (tests import them eagerly).
Auto-wiring a file handler on import also means every test run scribbles
into the user's real ``~/.xmclaw/logs/``, which is both surprising and
slow on Windows.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from xmclaw.utils.paths import get_logs_dir
from xmclaw.utils.redact import redact_string

# Rotation defaults. Override with env vars for local debugging if needed.
_MAX_BYTES = int(os.environ.get("XMC_LOG_MAX_BYTES", 5 * 1024 * 1024))  # 5 MiB
_BACKUP_COUNT = int(os.environ.get("XMC_LOG_BACKUPS", 3))

_CONFIGURED = False


def _scrub_secrets(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: run :func:`redact_string` on every string value.

    Covers the message body and any ``log.info("...", token=...)`` kwargs. We
    don't walk nested structures at log-emit time — callers logging a full
    response body should pre-redact with :func:`xmclaw.utils.redact.redact`.
    """
    for k, v in event_dict.items():
        if isinstance(v, str):
            event_dict[k] = redact_string(v)
    return event_dict


_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _resolve_log_level(cfg_level: str | None = None) -> int:
    """B-311: log level resolution priority.

    1. ``XMC_LOG_LEVEL`` env var (highest — debug knob for shell sessions)
    2. ``cfg_level`` argument (from config.json ``logging.level``)
    3. ``INFO`` (default)

    Accepts case-insensitive strings: DEBUG / INFO / WARNING / ERROR /
    CRITICAL. Unknown values fall back to INFO with a one-line stderr
    note (don't crash daemon over a typo).
    """
    candidates = [
        os.environ.get("XMC_LOG_LEVEL"),
        cfg_level,
    ]
    for raw in candidates:
        if not raw:
            continue
        norm = str(raw).strip().upper()
        if norm in _VALID_LEVELS:
            return getattr(logging, norm)
        sys.stderr.write(
            f"[xmclaw.log] WARNING: ignoring invalid log level "
            f"{raw!r} (expected one of {sorted(_VALID_LEVELS)})\n",
        )
    return logging.INFO


def setup_logging(cfg_level: str | None = None) -> structlog.stdlib.BoundLogger:
    """Idempotent structlog + stdlib-logging configuration.

    Returns the root bound logger. Calling a second time is a no-op — we
    don't want a test that touches logging to keep stacking handlers onto
    the root logger.

    B-311: ``cfg_level`` (optional) reads ``logging.level`` from
    config.json. Override priority: ``XMC_LOG_LEVEL`` env > cfg_level
    > INFO. Idempotent on the level too — a second setup_logging call
    won't change level (call site picks the level once at boot).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return structlog.get_logger()

    log_dir = get_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Wipe any handlers a previous (mis)configuration may have left; we own
    # the file + stream handlers here and want exactly one of each.
    root.handlers.clear()
    root.setLevel(_resolve_log_level(cfg_level))
    root.addHandler(
        RotatingFileHandler(
            log_dir / "xmclaw.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    )
    root.addHandler(logging.StreamHandler())
    for h in root.handlers:
        h.setFormatter(logging.Formatter("%(message)s"))

    structlog.configure(
        processors=[
            # contextvars first so downstream processors see bound fields.
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            # Scrub secrets last-but-one: after structlog has assembled the
            # full event_dict, before it's rendered to JSON.
            _scrub_secrets,
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True
    return structlog.get_logger()


def set_log_level(cfg_level: str | None = None) -> None:
    """B-311: live-adjust the root logger level.

    Use after ``setup_logging()`` once config.json is parsed. Safe to
    call multiple times; just sets ``logging.getLogger().level``. Does
    NOT touch handlers or processors.
    """
    logging.getLogger().setLevel(_resolve_log_level(cfg_level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger without triggering global setup.

    Modules that want to log should call this; the daemon entry point is
    responsible for calling :func:`setup_logging` once at startup. Before
    setup, structlog returns a logger that emits to the default handler —
    good enough for imports to stay side-effect-free.
    """
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_log_context(**kwargs: Any) -> None:
    """Pin ``session_id`` / ``agent_id`` / etc. on the current contextvars.

    Bound fields are merged into every log record emitted from this async
    task / thread until cleared. Prefer this over threading ``session_id``
    through every function signature.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_log_context() -> None:
    """Drop all contextvar bindings. Call at turn end."""
    structlog.contextvars.clear_contextvars()


def rotate_if_large(
    log_file: Path,
    max_bytes: int = _MAX_BYTES,
    backups: int = _BACKUP_COUNT,
) -> None:
    """Rotate a plain (non-logging) log file before it's reopened.

    Used for files that are written via subprocess stdout redirection rather
    than the Python logging module — RotatingFileHandler doesn't apply there,
    so we do a one-shot rotation at each daemon start.

    Produces ``<log_file>``, ``<log_file>.1``, ..., ``<log_file>.<backups>``,
    with ``.1`` being the most recent rotation. Oldest backup is dropped.
    Safe to call when the file does not yet exist.
    """
    try:
        if not log_file.exists() or log_file.stat().st_size < max_bytes:
            return

        oldest = log_file.with_suffix(log_file.suffix + f".{backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(backups - 1, 0, -1):
            src = log_file.with_suffix(log_file.suffix + f".{i}")
            dst = log_file.with_suffix(log_file.suffix + f".{i + 1}")
            if src.exists():
                src.rename(dst)

        log_file.rename(log_file.with_suffix(log_file.suffix + ".1"))
    except OSError:
        # Rotation is best-effort; never block daemon startup on log cleanup.
        pass
