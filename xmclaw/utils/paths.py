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


def skills_dir() -> Path:
    """Where ``SkillRegistry`` persists promote/rollback JSONL history.

    One file per skill: ``<skills>/<skill_id>.jsonl``. Peer of ``v2/``
    rather than nested inside it, so a daemon-workspace wipe
    (``rm -rf ~/.xmclaw/v2``) does not erase the skill evolution log —
    that's audit data and must survive session resets.
    """
    override = os.environ.get("XMC_V2_SKILLS_DIR")
    if override:
        return Path(override)
    return data_dir() / "skills"


def persona_dir() -> Path:
    """Persona profile markdown files — ``<data>/persona/profiles/``.

    Epic #18 router surface: ``GET /api/v2/profiles`` reads from here.
    Peer of ``v2/`` so persona files survive a daemon-workspace wipe
    (they're user content, not daemon state).
    """
    return data_dir() / "persona" / "profiles"


def workspaces_dir() -> Path:
    """Persisted workspace configs — ``<data>/workspaces/*.json``.

    Epic #18 router surface: ``GET/POST/DELETE /api/v2/workspaces``.
    Not to be confused with :func:`v2_workspace_dir` (the daemon's
    runtime state dir). These are user-authored agent personas /
    tool-preset bundles that the web UI edits.
    """
    return data_dir() / "workspaces"


def agents_registry_dir() -> Path:
    """Running multi-agent registry — ``<data>/v2/agents/*.json``.

    Epic #17 Phase 2 MultiAgentManager persists here. Distinct from
    :func:`workspaces_dir` on purpose — that directory holds abstract
    user presets (name / description / model) edited by the web UI,
    while this one holds the fully-resolved runtime config each
    live ``Workspace`` was built from (llm keys, tools list, security
    policy, agent_id). Keeping them apart means hand-editing a preset
    in the UI can't corrupt a running agent's replay state, and a v2
    workspace wipe (``rm -rf ~/.xmclaw/v2``) can reset all running
    agents without touching the user's preset library.
    """
    return v2_workspace_dir() / "agents"


def evolution_dir() -> Path:
    """Per-agent evolution audit trail — ``<data>/v2/evolution/<agent_id>/``.

    Epic #17 Phase 7 EvolutionAgent writes ``decisions.jsonl`` here.
    Peer of :func:`agents_registry_dir` (``<data>/v2/agents``) on purpose:
    both belong to the v2 runtime subtree so a workspace wipe
    (``rm -rf ~/.xmclaw/v2``) clears both in one shot — evolution
    decisions are daemon state, not user-authored content that needs
    to survive a reset. The per-agent subdirectory is created lazily
    by :class:`xmclaw.daemon.evolution_agent.EvolutionAgent` when it
    first writes a decision.
    """
    return v2_workspace_dir() / "evolution"


def file_memory_dir() -> Path:
    """User-editable markdown memory — ``<data>/memory/*.md``.

    Epic #18 router surface: ``GET/POST /api/v2/memory`` reads and
    writes here. Distinct from the SQLite-vec long-term memory at
    :func:`default_memory_db_path` — that one is daemon-managed, this
    one is human-authored notes the agent can consult.
    """
    return data_dir() / "memory"


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
