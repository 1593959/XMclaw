"""Unit tests for ``xmclaw.utils.log`` (Epic #15 phase 1).

Contract we care about:

* Importing ``xmclaw.utils.log`` does NOT create log files — utils is
  the bottom of the DAG and must stay pure on import per
  ``xmclaw/utils/AGENTS.md``. Regression guard: an earlier revision
  ran ``setup_logging()`` at module scope, which silently scribbled
  into the user's ``~/.xmclaw/logs/`` on every test run.
* ``setup_logging()`` is idempotent — calling it a second time does
  not stack handlers on the stdlib root logger (a classic source of
  duplicated log lines).
* The secret-scrubber processor replaces known API-key patterns in
  the message and kwargs. If a caller accidentally logs
  ``log.error("auth failed", token="sk-ant-...")`` the token must
  land on disk as ``sk-ant-***`` — the whole point of wiring
  ``redact.py`` into the pipeline.
* ``structlog.contextvars`` are merged into each emitted record, so
  pinning ``session_id`` once at turn start propagates automatically
  to every ``get_logger().info(...)`` call downstream.
"""
from __future__ import annotations

import importlib
import json
import logging
import logging.handlers
from pathlib import Path

import pytest
import structlog


@pytest.fixture
def log_mod(tmp_path, monkeypatch):
    """Fresh log module pointed at ``tmp_path / "logs"``.

    ``setup_logging`` caches its done-ness in a module global and
    ``structlog.configure`` mutates process-wide state — without this
    fixture tests would bleed into each other.
    """
    import xmclaw.utils.log as mod

    tmp_logs = tmp_path / "logs"
    monkeypatch.setattr(mod, "get_logs_dir", lambda: tmp_logs)
    # Reset global flag; reload would also work but is heavier.
    mod._CONFIGURED = False
    # Wipe stdlib root handlers; setup_logging assumes exclusive ownership.
    logging.getLogger().handlers.clear()
    structlog.contextvars.clear_contextvars()

    yield mod

    structlog.contextvars.clear_contextvars()
    logging.getLogger().handlers.clear()
    mod._CONFIGURED = False


def _log_file(log_mod) -> Path:
    return log_mod.get_logs_dir() / "xmclaw.log"


def test_import_has_no_side_effects(tmp_path, monkeypatch):
    """Importing the module must NOT create a log directory anywhere."""
    sentinel_logs = tmp_path / "sentinel_logs"
    # Pre-patch the path target; then reload and verify nothing got created.
    import xmclaw.utils.log as mod
    monkeypatch.setattr(mod, "get_logs_dir", lambda: sentinel_logs)
    importlib.reload(mod)
    assert not sentinel_logs.exists()


def test_setup_logging_is_idempotent(log_mod):
    log_mod.setup_logging()
    n1 = len(logging.getLogger().handlers)
    log_mod.setup_logging()
    n2 = len(logging.getLogger().handlers)
    log_mod.setup_logging()
    n3 = len(logging.getLogger().handlers)
    assert n1 == n2 == n3, (
        "setup_logging should not stack handlers on repeat calls"
    )


def test_setup_logging_creates_log_file_handler(log_mod):
    log_mod.setup_logging()
    handlers = logging.getLogger().handlers
    file_handlers = [
        h for h in handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    stream_handlers = [
        h for h in handlers
        # RotatingFileHandler inherits StreamHandler, so use `type is` not
        # isinstance to isolate the plain stderr one.
        if type(h) is logging.StreamHandler
    ]
    assert len(file_handlers) == 1
    assert len(stream_handlers) == 1


def test_secret_scrubber_redacts_api_keys_in_kwargs(log_mod):
    """``log.info("...", token="sk-ant-...")`` must not land on disk raw."""
    log_mod.setup_logging()
    log = log_mod.get_logger("test")
    log.info("auth attempt", token="sk-ant-abc123DEF456GHI789JKL012")

    body = _log_file(log_mod).read_text(encoding="utf-8")
    assert "sk-ant-abc123" not in body
    assert "sk-ant-***" in body


def test_secret_scrubber_redacts_in_message(log_mod):
    log_mod.setup_logging()
    log = log_mod.get_logger("test")
    log.warning("oops pasted key sk-ant-abc123DEF456GHI789JKL012 into msg")

    body = _log_file(log_mod).read_text(encoding="utf-8")
    assert "sk-ant-abc123" not in body
    assert "sk-ant-***" in body


def test_json_output_is_parseable(log_mod):
    log_mod.setup_logging()
    log = log_mod.get_logger("test")
    log.info("hello", foo=1, bar="two")

    line = _log_file(log_mod).read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "hello"
    assert rec["foo"] == 1
    assert rec["bar"] == "two"
    assert rec["level"] == "info"
    assert "timestamp" in rec


def test_contextvars_are_merged_into_records(log_mod):
    """Binding session_id once must propagate to every downstream log call."""
    log_mod.setup_logging()
    log_mod.bind_log_context(session_id="s_42", agent_id="a_7")
    log = log_mod.get_logger("test")
    log.info("inside turn")

    line = _log_file(log_mod).read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["session_id"] == "s_42"
    assert rec["agent_id"] == "a_7"


def test_clear_log_context_drops_bindings(log_mod):
    log_mod.setup_logging()
    log_mod.bind_log_context(session_id="s_99")
    log_mod.clear_log_context()
    log = log_mod.get_logger("test")
    log.info("after clear")

    line = _log_file(log_mod).read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert "session_id" not in rec


def test_get_logger_returns_structlog_instance(log_mod):
    log = log_mod.get_logger("test")
    assert hasattr(log, "info")
    assert hasattr(log, "bind")
