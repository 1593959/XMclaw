"""Central path resolution for XMclaw.

§3.1 of the dev roadmap requires every runtime path to resolve through
this module — no other caller hand-builds ``~/.xmclaw/v2/...`` strings.
The point is that a single lever (``XMC_DATA_DIR`` env var, a future
``--data-dir`` CLI flag) can reroute the entire install, which is how
Docker mounts / portable installs / test harnesses stay sane.

**Env vars honored**, in priority order:

* ``XMC_DATA_DIR`` — workspace root, the directory that would otherwise
  be ``~/.xmclaw``. Every v2 file lives under ``<root>/v2/``; logs live
  under ``<root>/logs/``.
* Narrow overrides for a single file (e.g. ``XMC_V2_PAIRING_TOKEN_PATH``)
  still win for that specific path. Used by pytest so one fixture can
  reroute only the token without moving everything.

The legacy ``BASE_DIR`` / ``get_*_dir()`` API from v1 is retained only
so ``xmclaw/utils/security.py`` (path-safety check, not runtime writes)
keeps working. New callers should use the functions below instead.
"""
from __future__ import annotations

import os
from pathlib import Path

# v1 legacy — pinned at module load so tests can monkeypatch confidently.
# Only the path-safety check in ``utils/security.py`` reads this today.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ── v2 API (use these for any new code) ─────────────────────────────────

def data_dir() -> Path:
    """Workspace root. Defaults to ``~/.xmclaw``; honors ``XMC_DATA_DIR``.

    Every file the daemon reads or writes lives under here — the v2
    subtree at ``data_dir() / "v2"`` plus the structured logs at
    ``data_dir() / "logs"``. Keep this as the only way to bridge
    ``$HOME`` into XMclaw paths so ``XMC_DATA_DIR`` is a genuine lever.
    """
    override = os.environ.get("XMC_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".xmclaw"


def v2_workspace_dir() -> Path:
    """The ``v2/`` subtree under :func:`data_dir`."""
    return data_dir() / "v2"


def logs_dir() -> Path:
    """Structured-log destination — ``<data>/logs/``.

    Peer of ``v2/`` on purpose: logs survive a full v2 workspace wipe
    so an incident trail stays intact when the user runs ``xmclaw stop
    && rm -rf ~/.xmclaw/v2`` to reset the daemon.
    """
    return data_dir() / "logs"


def default_pid_path() -> Path:
    """PID file the daemon writes during ``xmclaw start``.

    Honors the narrow ``XMC_V2_PID_PATH`` override first (used by the
    daemon-lifecycle test fixtures to isolate one run's PID file).
    """
    override = os.environ.get("XMC_V2_PID_PATH")
    if override:
        return Path(override)
    return v2_workspace_dir() / "daemon.pid"


def default_meta_path() -> Path:
    """Sidecar to the PID file — host/port/version of the live daemon."""
    return v2_workspace_dir() / "daemon.meta"


def default_daemon_log_path() -> Path:
    """Raw stdout/stderr capture from ``xmclaw start``.

    Distinct from :func:`logs_dir` — that one holds structlog JSON; this
    one is a plain text tee of the subprocess output. ``xmclaw stop``
    / ``restart`` / ``doctor`` all read it for crash post-mortem.
    """
    return v2_workspace_dir() / "daemon.log"


def default_token_path() -> Path:
    """Pairing token. Anti-req #8 enforcement point.

    Honors the narrow ``XMC_V2_PAIRING_TOKEN_PATH`` env var first so
    pytest can reroute only this file (the whole workspace doesn't need
    to move just to isolate a single test harness).
    """
    override = os.environ.get("XMC_V2_PAIRING_TOKEN_PATH")
    if override:
        return Path(override)
    return v2_workspace_dir() / "pairing_token.txt"


def default_events_db_path() -> Path:
    """SQLite event log — the event-replay + audit-trail substrate.

    Honors the narrow ``XMC_V2_EVENTS_DB_PATH`` override first (used by
    the doctor's health check for installed-package probing).
    """
    override = os.environ.get("XMC_V2_EVENTS_DB_PATH")
    if override:
        return Path(override)
    return v2_workspace_dir() / "events.db"


def default_memory_db_path() -> Path:
    """SQLite-vec memory store — the agent's long-term memory."""
    return v2_workspace_dir() / "memory.db"


# ── v1 legacy (do not extend; kept so existing callers keep working) ────

def get_agent_dir(agent_id: str) -> Path:
    return BASE_DIR / "agents" / agent_id


def get_shared_dir() -> Path:
    return BASE_DIR / "shared"


def get_logs_dir() -> Path:
    """Legacy v1 name. Delegates to :func:`logs_dir`.

    v1 returned ``BASE_DIR / "logs"`` (inside the repo), which violated
    §3.1 "repo MUST NOT hold runtime data". ``log.py``'s docstring
    already claimed ``~/.xmclaw/logs/``, so this realigns behaviour
    with the advertised contract.
    """
    return logs_dir()


def get_tmp_dir() -> Path:
    return BASE_DIR / "tmp"


def get_cache_dir() -> Path:
    return BASE_DIR / "cache"
