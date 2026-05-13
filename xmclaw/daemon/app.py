"""v2 daemon — FastAPI app exposing the v2 event bus over WebSocket.

Phase 4.0 delivery. Minimal end-to-end app: health check + one WS
endpoint that proxies user messages into the bus and streams
behavioral events back out as NDJSON frames. LLM wiring is NOT here
yet — Phase 4.1 layers the scheduler / grader / skills stack on top.

This is the first place v2 emerges as a RUNNING SERVICE rather than a
test-harness: ``xmclaw v2 serve`` starts it, and any WS client can
connect.

Anti-req #8 (device-bound auth on WS) stays advisory here —
``auth_check`` argument on the factory hooks in the enforcement path.
Phase 4.x replaces the default accept-all with ed25519 pairing.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from xmclaw import __version__
from xmclaw.daemon.agent_context import (
    AgentContextMiddleware,
    use_current_agent_id,
)
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.multi_agent_manager import MultiAgentManager
from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    SqliteEventBus,
    event_as_jsonable,
    make_event,
)
from xmclaw.daemon.app_lifespan import make_lifespan
from xmclaw.utils.log import get_logger

# Epic #24 Phase 1: module-level logger. Several pre-existing
# error-path call sites used a bare ``log.warning(...)`` reference
# without importing one — pure pre-existing NameError bug that
# would surface on first channel / MCP failure. Defining this here
# unblocks lint and makes those branches actually log.
log = get_logger(__name__)


_SECRET_KEYS = frozenset({
    "api_key", "apikey", "bot_token", "app_token", "token",
    "password", "secret", "authorization",
})


def _find_skill_provider(root: Any) -> tuple[Any, Any]:
    """Walk a tool-provider tree, return ``(skill_tool_provider, registry)``.

    B-298: the factory wraps tools as
    ``CompositeToolProvider(CompositeToolProvider(SkillTool, ...),
    MemoryBridge)``, so the SkillToolProvider can sit two levels
    deep. Pre-B-298 single-level lookup against the wrong attribute
    name (``_providers``) silently returned ``(None, None)``,
    leaving:

    * the EvolutionAgent without a registry → B-296's per-skill
      HEAD inference (``registry.active_version(skill_id)``)
      degraded to ``head_version=None`` for every skill;
    * the VariantSelector's ``enabled`` branch never taken →
      candidates never got explore-traffic → controller's
      ``min_plays`` threshold never cleared → B-294's evaluate()
      trigger fired but always proposed nothing.

    Returns the first node in a depth-first traversal whose
    ``_registry`` attribute looks like a SkillRegistry (has the
    ``list_skill_ids`` method). ``(None, None)`` when no such
    provider exists in the tree (e.g. echo-mode daemon, or an
    agent assembled with only BuiltinTools).

    The walker tries public attribute names first
    (``children``) then private ones (``_children``) so a
    custom ToolProvider that exposes a public children API
    can override the walk without subclassing
    CompositeToolProvider.
    """
    stack = [root] if root is not None else []
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        reg = getattr(node, "_registry", None)
        if reg is not None and hasattr(reg, "list_skill_ids"):
            return node, reg
        kids = (
            getattr(node, "children", None)
            or getattr(node, "_children", None)
            or getattr(node, "_providers", None)
            or []
        )
        for k in kids:
            stack.append(k)
    return None, None


def _sanitize_config(cfg: Any) -> Any:
    """Deep-copy ``cfg`` with secret-shaped values redacted.

    Any dict key matching ``_SECRET_KEYS`` (case-insensitive) gets its
    value replaced with a short fingerprint like ``"sk-***4chars"`` so
    the UI can confirm a key is SET without leaking it.
    """
    if isinstance(cfg, dict):
        out: dict[str, Any] = {}
        for k, v in cfg.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                if isinstance(v, str) and v:
                    tail = v[-4:] if len(v) > 4 else ""
                    out[k] = f"<redacted …{tail}>"
                else:
                    out[k] = "<unset>"
            else:
                out[k] = _sanitize_config(v)
        return out
    if isinstance(cfg, list):
        return [_sanitize_config(x) for x in cfg]
    return cfg


def _restore_secrets(existing: Any, incoming: Any) -> Any:
    """Inverse of :func:`_sanitize_config`.

    The ConfigPage form submits its current view, which has redacted
    secret fields from the previous GET. When the user edits a non-
    secret field, the redacted secret is round-tripped back to us as
    ``"<redacted …xxxx>"`` or ``"<unset>"``. This restores the real
    secret from ``existing`` so a save doesn't wipe API keys. Users who
    actually want to change the secret send the new plaintext value
    (front-end clears the field first).
    """
    if isinstance(existing, dict) and isinstance(incoming, dict):
        out: dict[str, Any] = {}
        for k, v in incoming.items():
            ev = existing.get(k) if isinstance(k, str) else None
            if (
                isinstance(k, str)
                and k.lower() in _SECRET_KEYS
                and isinstance(v, str)
                and (v.startswith("<redacted ") or v == "<unset>")
            ):
                # Keep the existing secret untouched.
                out[k] = ev if ev is not None else ""
            else:
                out[k] = _restore_secrets(ev, v)
        return out
    if isinstance(existing, list) and isinstance(incoming, list):
        # Lists: best-effort element-wise restore for same-length lists,
        # else trust the incoming list.
        if len(existing) == len(incoming):
            return [_restore_secrets(e, i) for e, i in zip(existing, incoming)]
        return list(incoming)
    return incoming


# Module-level handle to the active app's ``state``. Populated late in
# ``create_app`` (after agent wiring) so factory-time closures — most
# importantly the persona writeback used by self-modification tools —
# can reach the live agent without an explicit Request. ``None`` until
# ``create_app`` runs; tests that import this module without booting
# the FastAPI app see the bare default and treat it as "no agent".
_LAST_APP_STATE: Any = None

# B-70: hold strong refs to background session-reflection tasks
# fired from the WS disconnect path. Same pattern as B-68/B-69 —
# asyncio's weak-ref tracking lets fire-and-forget tasks get GC'd
# mid-flight, dropping the reflection LLM call silently. Each task
# adds itself + auto-removes on done via the wrapper below.
_PENDING_REFLECTIONS: set[Any] = set()


async def _run_session_reflection(
    agent: Any, session_id: str, msg_count: int,
) -> None:
    """Fire a self-prompted reflection turn after a substantive session.

    Called from the WS close handler when ``msg_count`` clears a
    threshold. Spawns a fresh session id (``reflect:<sid>:<ts>``) so
    the reflection doesn't pollute the user's transcript, but the
    agent still has its full history of the just-closed session
    available because it copies the history into the new session id
    before running the turn.

    The agent is asked to be conservative — most sessions don't
    produce durable insights, and we don't want MEMORY.md to bloat
    with one-off chitchat.
    """
    try:
        # Copy the closing session's history into the reflect session
        # so the agent can read what was discussed. AgentLoop keeps
        # histories in self._histories (in-memory dict).
        import time as _time
        reflect_sid = f"reflect:{session_id}:{int(_time.time())}"
        try:
            prior = list(agent._histories.get(session_id, []))  # noqa: SLF001
            if prior:
                agent._histories[reflect_sid] = prior  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass

        prompt = (
            "Session reflection — the user just disconnected from "
            f"session {session_id} ({msg_count} messages). "
            "Look back over the conversation in your history. Ask "
            "yourself: did anything DURABLE come up that should "
            "survive into next conversation?\n\n"
            "Triggers worth writing:\n"
            "  - User stated a preference (terse vs detailed, "
            "language, naming, etc.) → learn_about_user\n"
            "  - User shared a fact about themselves or their "
            "project → learn_about_user\n"
            "  - We made a decision together (\"we'll use X not "
            "Y\") → remember (category: \"Decisions\")\n"
            "  - I learned a project convention worth remembering "
            "→ remember (category: \"Project conventions\")\n\n"
            "Triggers NOT worth writing:\n"
            "  - One-off requests / completed tasks (those leave "
            "their own artifacts, no need to record)\n"
            "  - Standard back-and-forth (\"can you read this "
            "file\") — totally fine, just not memory-worthy\n\n"
            "If nothing durable came up, just reply 'no notes' — "
            "do not write to MEMORY.md or USER.md. Otherwise, "
            "call remember / learn_about_user (or update_persona) "
            "with one or two well-targeted entries. Be terse — "
            "MEMORY.md is supposed to age well."
        )
        await agent.run_turn(reflect_sid, prompt)
    except Exception as exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "session.reflection_failed",
            extra={"session_id": session_id, "err": str(exc)},
        )

    # Real-time evolution now flows through JournalWriter +
    # ProfileExtractor + RealtimeEvolutionTrigger (post-LLM_RESPONSE
    # debounced) — all event-driven via the bus, all gated by
    # HonestGrader. This anonymous reflection task focuses purely on
    # session-end memory curation.

    # B-28 on_session_end hook: fan out to every memory provider so
    # they can do end-of-session fact extraction / summarisation.
    # Hindsight calls client.flush; sqlite_vec is a no-op default;
    # builtin_file ignores. The reflection step above already covered
    # MEMORY.md / USER.md curation via the agent's own tools — this
    # hook is the LOWER-LEVEL post-session signal for backends that
    # batch their writes.
    try:
        mgr = getattr(agent, "_memory_manager", None) if agent is not None else None
        if mgr is not None:
            # Pull a serialisable copy of the closed session's history.
            try:
                history = list(agent._histories.get(session_id, []))  # noqa: SLF001
            except Exception:  # noqa: BLE001
                history = []
            history_dicts = []
            for m in history:
                d = {"role": getattr(m, "role", "?")}
                c = getattr(m, "content", None)
                if isinstance(c, str):
                    d["content"] = c
                history_dicts.append(d)
            await mgr.on_session_end(
                session_id=session_id, messages=history_dicts,
            )
    except Exception as exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "session.on_session_end_failed",
            extra={"session_id": session_id, "err": str(exc)},
        )


def _origin_allowed(origin: str | None, cfg: dict) -> bool:
    """B-355: validate the ``Origin`` header for WebSocket upgrades
    AND ``/api/v2/*`` mutating HTTP requests. Defense against
    ClawJacked-style attacks (malicious page in user's browser
    fetching loopback daemon).

    Allowed by default (returned True):
      * No origin header at all (CLI / SDK / curl — they don't send
        Origin)
      * ``null`` (file://, native shells, sandboxed iframes)
      * ``http://127.0.0.1:*`` / ``http://localhost:*`` /
        ``http://[::1]:*`` (loopback browser)
      * ``https://127.0.0.1:*`` / etc (TLS loopback)
      * Any origin in ``gateway.allowed_origins`` (config opt-in).

    Everything else (``http://evil.com``, ``http://192.168.x.x:*``)
    is rejected. Operators wanting to expose to a LAN must
    explicitly opt in by listing the LAN origin.
    """
    if not origin or origin == "null":
        return True
    # Parse scheme + host.
    try:
        from urllib.parse import urlparse
        p = urlparse(origin)
        host = (p.hostname or "").lower()
    except Exception:  # noqa: BLE001 — malformed origin → reject
        return False
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    # Config opt-in.
    gw_cfg = (cfg or {}).get("gateway") or {}
    extras = gw_cfg.get("allowed_origins") or []
    if isinstance(extras, list):
        for o in extras:
            if isinstance(o, str) and origin == o.rstrip("/"):
                return True
    return False


def create_app(
    *,
    bus: InProcessEventBus | None = None,
    auth_check: Callable[[str | None], Awaitable[bool]] | None = None,
    agent: AgentLoop | None = None,
    config: dict[str, Any] | None = None,
    config_path: Path | None = None,
    orchestrator: Any | None = None,
) -> FastAPI:
    """Build the v2 FastAPI app.

    Parameters
    ----------
    bus : InProcessEventBus | None
        Event bus to use. Defaults to a fresh in-process instance so
        each ``create_app`` call gets an isolated bus — useful for
        tests. Production callers should pass a shared bus.
    auth_check : callable | None
        Async ``(token: str | None) -> bool`` for anti-req #8 pairing.
        The server extracts the token from either the ``token`` query
        parameter or an ``Authorization: Bearer <token>`` header. When
        ``auth_check`` is set, a missing or failed token closes the WS
        with code 4401. Default (``None``) accepts all connections —
        safe only on loopback.
    agent : AgentLoop | None
        Optional agent turn orchestrator. When provided, user messages
        trigger ``agent.run_turn`` (LLM ↔ tool loop); events flow back
        via the bus subscription.
    config : dict | None
        Optional config dict (``daemon/config.json`` shape). If
        ``agent`` is not provided but ``config`` is, the factory tries
        to build an AgentLoop from the config's LLM section. This is
        the usable-out-of-the-box path for ``xmclaw v2 serve``.
    orchestrator : EvolutionOrchestrator | None
        Epic #4 Phase C. Optional bus-aware wrapper over
        :class:`xmclaw.skills.registry.SkillRegistry`. When provided,
        the daemon starts it on lifespan-enter and stops it on
        shutdown. ``auto_apply=True`` orchestrators then consume
        ``SKILL_CANDIDATE_PROPOSED`` events and mutate HEAD; the
        resulting ``SKILL_PROMOTED`` / ``SKILL_ROLLED_BACK`` events
        flow back onto every connected REPL via ``_GLOBAL_EVENT_TYPES``.
        Typed as ``Any`` so ``xmclaw/daemon/`` respects the "must not
        import xmclaw.skills" boundary (see ``xmclaw/daemon/AGENTS.md``);
        the orchestrator is built upstream by the CLI and handed in.

    Precedence: explicit ``agent=`` wins over ``config=``. If neither
    is given, the daemon runs in Phase 4.0 echo mode — useful for
    WS-plumbing tests and clients that manage their own reasoning
    upstream.
    """
    bus = bus or InProcessEventBus()
    memory = None
    # B-395 (Sprint 1): capture the actual exception when memory build
    # fails so the SetupBanner can stop guessing. Pre-B-395 the bare
    # except dropped the error string and the indexer block fell back
    # to a generic ``memory.enabled=false 或构造失败 — 检查 memory.* 节``
    # message, which is wrong when ``memory.enabled`` IS true. The
    # most common real cause on Windows is sqlite_vec unable to load
    # its native extension; the user followed the wrong fix-list (delete
    # memory.db) for hours instead of running ``pip install sqlite-vec``.
    memory_build_error: str | None = None
    sweep_task = None
    backup_scheduler = None
    if config is not None:
        from xmclaw.daemon.factory import build_memory_from_config
        from xmclaw.daemon.memory_sweep import (
            MemorySweepTask,
            parse_retention_config,
        )
        try:
            memory = build_memory_from_config(config, bus=bus)
        except Exception as exc:  # noqa: BLE001 — malformed memory config must not block daemon
            memory = None
            memory_build_error = (
                f"{type(exc).__name__}: {exc}"
            )
        if memory is not None:
            retention = parse_retention_config(
                (config.get("memory") or {}).get("retention")
                if isinstance(config.get("memory"), dict)
                else None
            )
            sweep_task = MemorySweepTask(memory, retention)

        # Epic #20 Phase 2: auto-daily workspace backup. Disabled by
        # default (policy.auto_daily=False ⇒ start() no-ops). Kept
        # independent of the memory-retention sweep so a daemon can opt
        # into one without the other.
        from xmclaw.daemon.backup_scheduler import (
            BackupSchedulerTask,
            parse_backup_config,
        )
        backup_policy = parse_backup_config(config.get("backup"))
        if backup_policy.auto_daily:
            backup_scheduler = BackupSchedulerTask(
                source_dir=None,  # defer to utils.paths.data_dir() at tick time
                policy=backup_policy,
            )

    # Jarvisification Phase 5: load shared cognitive state early so
    # MultiAgentManager can hand it to every sub-agent.  Persistence
    # happens at lifespan shutdown.
    _cognition_cfg = (config or {}).get("cognition") or {}
    _shared_cognitive_state = None
    if _cognition_cfg.get("enabled", True):
        try:
            from xmclaw.cognition.state import CognitiveState
            from xmclaw.utils.paths import default_cognitive_state_path
            _state_path = default_cognitive_state_path()
            if _state_path.exists():
                import json
                _data = json.loads(_state_path.read_text(encoding="utf-8"))
                _shared_cognitive_state = CognitiveState.from_dict(_data)
                log.info("cognition.state_loaded path=%s", _state_path)
            else:
                _shared_cognitive_state = CognitiveState()
        except Exception as exc:  # noqa: BLE001
            log.warning("cognition.state_load_failed err=%s", exc)
            from xmclaw.cognition.state import CognitiveState
            _shared_cognitive_state = CognitiveState()

    # Epic #17 Phase 3: multi-agent registry. Constructed eagerly so the
    # routers and WS handler can rely on ``app.state.agents`` being set,
    # but rehydration from disk happens in lifespan so tests that never
    # enter lifespan don't pay the filesystem walk.
    # B-134: pass the primary config so sub-agents can inherit its llm
    # block when their own preset omits one (persona templates ship
    # only system_prompt; provider/model fall through from main).
    # Phase 5: shared cognitive substrate across all agents.
    agents_manager = MultiAgentManager(
        bus,
        primary_config=config,
        cognitive_state=_shared_cognitive_state,
    )

    # Phase 6 cron: stand up a CronTickTask once the agent is wired so
    # ~/.xmclaw/cron/jobs.json actually fires every 60s. Runner uses
    # the primary AgentLoop's run_turn to execute the job's prompt.

    # 进化路径设计原则: 所有 skill 提案都必须过 HonestGrader 的
    # 0.80 hard-evidence 评分 (ran/returned/type_matched/side_effect)
    # + 0.20 LLM cap, 然后通过 EvolutionAgent → controller →
    # orchestrator → SkillRegistry.promote(evidence=...) 的链路落地.
    # anti-req #12 在 registry 门口强制 evidence 非空, 杜绝 "agent
    # 总以为自己干得不错" 的失败模式。

    # B-309: events.db retention. Deletes events older than N days
    # daily + runs incremental vacuum so the file doesn't grow
    # monotonically. Skipped when bus doesn't support prune (echo
    # mode or non-Sqlite bus).
    events_retention_task = None
    try:
        from xmclaw.daemon.events_retention import EventsRetentionTask
        _retention_cfg = (config or {}).get("events_retention", {}) or {}
        events_retention_task = EventsRetentionTask(
            bus,
            max_age_days=float(_retention_cfg.get("max_age_days", 30.0)),
            interval_hours=float(_retention_cfg.get("interval_hours", 24.0)),
            enabled=bool(_retention_cfg.get("enabled", True)),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("events_retention.build_failed err=%s", exc)
        events_retention_task = None

    _lifespan = make_lifespan(
        bus=bus, memory=memory, sweep_task=sweep_task,
        backup_scheduler=backup_scheduler, events_retention_task=events_retention_task,
        config=config, agent=agent, orchestrator=orchestrator,
        agents_manager=agents_manager, shared_cognitive_state=_shared_cognitive_state,
        cognition_cfg=_cognition_cfg, memory_build_error=memory_build_error,
        config_path=config_path,
    )

    app = FastAPI(
        title="XMclaw v2 daemon", version=__version__, lifespan=_lifespan,
    )
    # Epic #17 Phase 4: ambient "who am I?" contextvar. Seeded from
    # ``X-Agent-Id`` header or ``agent_id`` query param on every
    # HTTP/WS request. The WS handler overrides it per-turn with the
    # resolved id (so "main" and default-to-primary both normalize).
    app.add_middleware(AgentContextMiddleware)
    app.state.bus = bus
    app.state.memory = memory
    app.state.memory_sweep = sweep_task
    app.state.orchestrator = orchestrator
    # Stash the raw config on app.state so router surfaces (Epic #18)
    # can read ``tools.allowed_dirs`` without re-loading from disk and
    # without an import cycle through the factory.
    app.state.config = config or {}
    # Multi-model: routers/llm_profiles.py writes to this path on POST
    # /DELETE; if it's None the router returns 500 with an explanatory
    # error rather than guessing a write target.
    app.state.config_path = config_path
    # Populated below alongside the agent — kept None when the daemon
    # boots without an LLM (echo-only mode for tests).
    app.state.llm_registry = None

    # Epic #3: approval service for GuardedToolProvider needs_approval path.
    from xmclaw.security.approval_service import ApprovalService
    app.state.approval_service = ApprovalService()

    # Phase B security hardening: unified security audit log.
    from xmclaw.security.auditor import SecurityAuditor
    _security_auditor = SecurityAuditor()
    _security_auditor.subscribe_to_bus(bus)
    app.state.security_auditor = _security_auditor

    # Epic #18 Phase A: web-UI router surfaces (files / memory /
    # profiles / workspaces). Included here so the panels have real
    # data instead of the ``xmclaw_adapter.js`` mocks they used to hit.
    from xmclaw.daemon.routers import files as _files_router
    from xmclaw.daemon.routers import llm_profiles as _llm_profiles_router
    from xmclaw.daemon.routers import memory as _memory_router
    from xmclaw.daemon.routers import profiles as _profiles_router
    from xmclaw.daemon.routers import skills as _skills_router
    from xmclaw.daemon.routers import analytics as _analytics_router
    from xmclaw.daemon.routers import cron as _cron_router
    from xmclaw.daemon.routers import docs as _docs_router
    from xmclaw.daemon.routers import logs as _logs_router
    from xmclaw.daemon.routers import sessions as _sessions_router
    from xmclaw.daemon.routers import workspace as _workspace_router
    from xmclaw.daemon.routers import workspaces as _workspaces_router
    from xmclaw.daemon.routers import journal as _journal_router
    from xmclaw.daemon.routers import system as _system_router
    # ``/api/v2/skills`` is the canonical surface for skill listing +
    # promote/rollback; ``/api/v2/evolution/snapshot`` (B-301) for
    # the live in-memory chain status. No legacy routers.
    from xmclaw.daemon.routers import backup as _backup_router  # B-103
    from xmclaw.daemon.routers import secrets as _secrets_router  # B-104
    from xmclaw.daemon.routers import channels as _channels_router  # B-147
    from xmclaw.daemon.routers import evolution as _evolution_router  # B-301
    from xmclaw.daemon.routers import skill_marketplace as _skill_marketplace_router  # B-390
    from xmclaw.daemon.routers import cognition as _cognition_router
    app.include_router(_files_router.router)
    app.include_router(_llm_profiles_router.router)
    app.include_router(_memory_router.router)
    app.include_router(_profiles_router.router)
    app.include_router(_analytics_router.router)
    app.include_router(_cron_router.router)
    app.include_router(_docs_router.router)
    app.include_router(_logs_router.router)
    app.include_router(_sessions_router.router)
    app.include_router(_skills_router.router)
    app.include_router(_workspace_router.router)
    app.include_router(_workspaces_router.router)
    app.include_router(_journal_router.router)
    app.include_router(_system_router.router)
    app.include_router(_backup_router.router)
    app.include_router(_secrets_router.router)
    app.include_router(_channels_router.router)  # B-147
    app.include_router(_evolution_router.router)  # B-301
    app.include_router(_skill_marketplace_router.router)  # B-390 (Sprint 2)
    app.include_router(_cognition_router.router)

    # Phase 3: ASGI middleware for X-Agent-Id → ContextVar plumbing
    # (QwenPaw multi-agent convention #1). Stays a no-op for the
    # default "main" agent id, so existing single-agent flows aren't
    # affected.
    from xmclaw.daemon.middleware import AgentScopeMiddleware
    app.add_middleware(AgentScopeMiddleware)

    # B-73: pairing-token auth on HTTP API routes. The WS handler
    # already enforces ``auth_check``; without this middleware the
    # parallel HTTP surface (sessions / config / memory / agents / …)
    # was wide open to anything on localhost — so a curl from any
    # process on the user's machine could read full chat history,
    # rewrite the daemon config, or delete sessions. Skipped when
    # ``auth_check is None`` (--no-auth daemon mode).
    if auth_check is not None:
        from xmclaw.daemon.middleware import PairingAuthMiddleware
        app.add_middleware(PairingAuthMiddleware, auth_check=auth_check)

    # B-75: cap request body size on /api/v2/* at 10 MB. ``request.json()``
    # buffers the entire body in memory before parsing — a 1 GB POST to
    # /api/v2/memory/<filename> or /api/v2/profiles/<canonical> would
    # OOM the daemon process. The cap covers every legitimate XMclaw
    # write (persona files, journal entries, notes, workspace manifests
    # all live in the KB-to-low-MB range). Always installed, even in
    # --no-auth mode, because OOM defence is orthogonal to authn.
    from xmclaw.daemon.middleware import BodySizeLimitMiddleware
    app.add_middleware(BodySizeLimitMiddleware)

    # Epic #17 Phase 3: REST surface for the multi-agent registry.
    from xmclaw.daemon.routers import agents as _agents_router
    app.include_router(_agents_router.router)

    # Epic #3: REST surface for security approvals.
    from xmclaw.daemon.routers import approvals as _approvals_router
    app.include_router(_approvals_router.router)

    app.state.agents = agents_manager

    if agent is None and config is not None:
        # Local import avoids a circular dep (factory imports from this
        # module's sibling packages).
        from xmclaw.daemon.factory import build_agent_from_config
        agent = build_agent_from_config(
            config, bus,
            approval_service=app.state.approval_service,
            auditor=getattr(app.state, "security_auditor", None),
        )

    # Epic #17 Phase 5: attach the agent-to-agent tools to the primary
    # loop so its LLM can call ``list_agents`` / ``chat_with_agent`` /
    # ``submit_to_agent`` / ``check_agent_task``. Done post-hoc here
    # (not inside the factory) because the agent-inter tools need a
    # reference to BOTH the manager and the primary loop — and the
    # primary loop doesn't exist yet when ``build_tools_from_config``
    # runs. Worker agents created via ``POST /api/v2/agents`` don't
    # currently get these tools: they're "delegates" in the initial
    # design, not "delegators". Revisit when a recursion use-case
    # shows up.
    if agent is not None and hasattr(agent, "_tools"):
        # hasattr guard: test fixtures pass stub agents that don't
        # implement the full AgentLoop surface. For those, skip —
        # the agent-inter tools only matter when a real loop is wired.
        from xmclaw.providers.tool.agent_inter import AgentInterTools
        from xmclaw.providers.tool.composite import CompositeToolProvider
        _inter = AgentInterTools(
            manager=agents_manager,
            primary_loop=agent,
            task_scheduler=getattr(app.state, "task_scheduler", None),
            swarm_orchestrator=getattr(app.state, "swarm_orchestrator", None),
        )
        if agent._tools is None:
            agent._tools = _inter
        else:
            agent._tools = CompositeToolProvider(agent._tools, _inter)

        # B-135: content tools — screenshot / pdf_read / docx_read /
        # xlsx_read / clipboard_read|write / image_read. Each tool
        # degrades gracefully (returns ok=False) when its optional
        # dep isn't installed; the daemon never refuses to boot.
        from xmclaw.providers.tool.content import ContentTools
        agent._tools = CompositeToolProvider(agent._tools, ContentTools())

        # B-136: automation tools — cron CRUD (5 tools) +
        # code_python + process_list/kill. Same wiring pattern as
        # ContentTools — graceful degradation on missing optional
        # deps (psutil for process_*).
        from xmclaw.providers.tool.automation import AutomationTools
        agent._tools = CompositeToolProvider(agent._tools, AutomationTools())

        # B-143: integration tools — webhook / email / rss /
        # slack / telegram / discord / github / notion. Each reads
        # its credentials from config.integrations.<service>.* and
        # surfaces 'configure first' when not set up. Closes the
        # 'integrations are stubs' gap the user flagged.
        from xmclaw.providers.tool.integrations import IntegrationsTools
        agent._tools = CompositeToolProvider(
            agent._tools,
            IntegrationsTools((config or {}).get("integrations")),
        )

        # B-124: bridge SkillRegistry HEAD entries into the tool surface.
        # SkillToolProvider is the **only** way a skill becomes callable
        # by the LLM — every version exposed here passed through
        # evidence-gated promote() (anti-req #12 enforced at registry).
        if orchestrator is not None:
            from xmclaw.skills.tool_bridge import SkillToolProvider
            _skill_tools = SkillToolProvider(orchestrator.registry)
            agent._tools = CompositeToolProvider(agent._tools, _skill_tools)

    app.state.agent = agent
    # Module-level handle so factory-time callbacks (the persona
    # writeback used by ``remember`` / ``learn_about_user`` /
    # ``update_persona``) can find the live agent without needing a
    # FastAPI Request object. Stored as the *state* object, not the
    # whole app, because the closures only need state attributes.
    global _LAST_APP_STATE
    _LAST_APP_STATE = app.state
    # Expose the multi-model registry so routers/llm_profiles.py can
    # enumerate live profiles without reaching into AgentLoop internals.
    if agent is not None:
        app.state.llm_registry = getattr(agent, "_llm_registry", None)
    # Jarvisification: expose memory_graph on app.state so shutdown
    # can close it cleanly.
    if agent is not None:
        _mem_mgr = getattr(agent, "_memory_manager", None)
        if _mem_mgr is not None:
            app.state.memory_graph = getattr(_mem_mgr, "_graph", None)

    # ── per-session event log (for reconnect replay) ─────────────
    # When a browser refresh disconnects and reconnects to the same
    # session_id, the client has an empty chat div -- live events
    # alone can't repopulate the transcript. So we tap the bus with a
    # global subscriber and keep a bounded log per session_id. On WS
    # connect, we stream the log first, then go live.
    _SESSION_LOG_CAP = 400  # events per session; ~20 turns of back-and-forth
    session_logs: dict[str, list[BehavioralEvent]] = {}

    # B-348 (Sprint 1): single-tab-wins per session. When a second
    # browser tab connects to the same session_id, the older tab's
    # WS gets a "superseded" frame and is closed. Without this, both
    # tabs subscribe to the bus, both receive every event, and the
    # bus fanout doubles for every additional tab — turn cancellation
    # also gets confusing because either tab can fire it. The session
    # log replay on reconnect already lets the new tab repopulate, so
    # closing the old WS doesn't lose state — just the live socket.
    active_ws_for_session: dict[str, WebSocket] = {}

    async def _session_log_subscriber(event: BehavioralEvent) -> None:
        buf = session_logs.setdefault(event.session_id, [])
        buf.append(event)
        if len(buf) > _SESSION_LOG_CAP:
            # Drop oldest. Matches agent_loop history_cap trimming spirit:
            # keep the recent transcript intact, sacrifice the archaeology.
            del buf[:len(buf) - _SESSION_LOG_CAP]

    bus.subscribe(lambda e: True, _session_log_subscriber)
    app.state.session_logs = session_logs

    @app.get("/health")
    async def health() -> JSONResponse:
        """Cheap liveness probe — confirms the app is responsive."""
        return JSONResponse({
            "status": "ok",
            "version": __version__,
            "bus": type(bus).__name__,
        })

    # B-215: silence favicon.ico 404 noise. We don't ship one (the
    # branding work is in /ui/ds-assets/) and every browser tab pollutes
    # daemon.log + the user's DevTools console with the 404. Empty 204
    # is the canonical "no favicon" response.
    #
    # B-322: ``include_in_schema=False`` keeps the route off the OpenAPI
    # spec. Without it, FastAPI / pydantic 2.12 walks the return-type
    # annotation through ``TypeAdapter``, and Starlette's ``Response``
    # isn't a pydantic-compatible model — it raised
    # ``PydanticUserError: TypeAdapter[ForwardRef('_PlainResponse')] is
    # not fully defined`` when ``/openapi.json`` was visited (broke the
    # router-mount integration test). Excluding this trivial 204 from
    # the schema is also the right semantic — favicon is a browser
    # concern, not an API surface.
    from starlette.responses import Response as _PlainResponse

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return _PlainResponse(status_code=204)

    # ── /api/v2/pair ──
    # Returns the pairing token (or null in --no-auth mode) to the UI
    # so users don't have to paste the token from disk. Security posture:
    # this endpoint has NO CORS headers set, so browsers enforce the
    # same-origin policy — a page at evil.com cannot fetch this URL
    # from the user's browser. Same-origin pages (our own UI at
    # /ui/*) can read it. Another process on localhost can curl this,
    # but that threat was already outside anti-req #8's scope (a
    # local-user process can also cat ~/.xmclaw/v2/pairing_token.txt).
    @app.get("/api/v2/pair")
    async def pair() -> JSONResponse:
        token: str | None = None
        if auth_check is not None:
            # Read the pairing file from the same location the daemon
            # created it in. Local import to avoid coupling the app
            # module to the pairing module's surface.
            try:
                from xmclaw.daemon.pairing import default_token_path
                token_path = default_token_path()
                if token_path.exists():
                    token = token_path.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                token = None
        return JSONResponse({"token": token})

    # ── /api/v2/config ────────────────────────────────────────────
    # Returns a sanitized view of the daemon's current config so the
    # "Run config" panel in the UI can show what the daemon actually
    # loaded. Redacts api_key / bot_token / password fields.
    @app.get("/api/v2/config")
    async def config_reflection() -> JSONResponse:
        if config is None:
            return JSONResponse({"config": None, "note": "running without a config file"})
        return JSONResponse({
            "config": _sanitize_config(config),
            "config_path": str(config_path) if config_path else None,
        })

    # ── PUT /api/v2/config ───────────────────────────────────────
    # Generic config writer used by the Hermes-style ConfigPage form.
    # Validates the body is a dict, then atomically writes it to the
    # on-disk config.json (preserving secrets the front-end can't see —
    # api_key / bot_token / password fields).
    @app.put("/api/v2/config")
    async def update_config(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            return JSONResponse(
                {"ok": False, "error": "body must be a JSON object"},
                status_code=400,
            )
        target_path = config_path or Path("daemon") / "config.json"
        target_path = Path(target_path)
        try:
            existing: dict[str, Any] = {}
            if target_path.exists():
                existing = json.loads(target_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            # Re-merge redacted fields the UI never received.
            merged = _restore_secrets(existing, payload)
            tmp = target_path.with_suffix(target_path.suffix + ".write.tmp")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            import os as _os
            _os.replace(tmp, target_path)
        except (OSError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=500,
            )
        # Update the in-memory config so subsequent requests see the new
        # values without a daemon restart.
        if config is not None:
            config.clear()
            config.update(merged)
        return JSONResponse({
            "ok": True,
            "config_path": str(target_path),
            "note": "restart daemon for LLM/runtime changes to take effect",
        })

    # ── PUT /api/v2/config/llm ─────────────────────────────────────
    # Front-end model configuration: write provider/api_key/base_url/
    # default_model into the on-disk config.json. Requires the daemon
    # to know its config path (CLI passes it via create_app); when
    # config was loaded from a dict but no path was given, falls back
    # to ``daemon/config.json`` relative to CWD so a fresh install can
    # still bootstrap from the UI without a CLI step.
    @app.put("/api/v2/config/llm")
    async def update_llm_config(payload: dict[str, Any]) -> JSONResponse:
        provider = payload.get("provider")
        if provider not in ("openai", "anthropic"):
            return JSONResponse(
                {"ok": False, "error": "provider must be 'openai' or 'anthropic'"},
                status_code=400,
            )
        api_key = str(payload.get("api_key", "") or "").strip()
        base_url = str(payload.get("base_url", "") or "").strip()
        default_model = str(payload.get("default_model", "") or "").strip()
        if not default_model:
            return JSONResponse(
                {"ok": False, "error": "default_model is required"},
                status_code=400,
            )

        target_path = config_path or Path("daemon") / "config.json"
        target_path = Path(target_path)

        if target_path.exists():
            try:
                current = json.loads(target_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return JSONResponse(
                    {"ok": False, "error": f"existing config is invalid JSON: {exc}"},
                    status_code=500,
                )
            if not isinstance(current, dict):
                current = {}
        else:
            current = {}

        llm_section = current.setdefault("llm", {})
        if not isinstance(llm_section, dict):
            llm_section = {}
            current["llm"] = llm_section
        llm_section["default_provider"] = provider
        prov_block = llm_section.setdefault(provider, {})
        if not isinstance(prov_block, dict):
            prov_block = {}
            llm_section[provider] = prov_block
        # Only overwrite api_key when caller provided a non-empty value;
        # an empty string in the form means "keep existing key" so the
        # user can edit base_url/model without re-entering the secret.
        if api_key:
            prov_block["api_key"] = api_key
        if base_url:
            prov_block["base_url"] = base_url
        prov_block["default_model"] = default_model

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(target_path)

        return JSONResponse({
            "ok": True,
            "path": str(target_path),
            "restart_required": True,
        })

    # ── /api/v2/status ────────────────────────────────────────────
    # Richer status than /health: active model, tool roster, mcp state.
    @app.get("/api/v2/status")
    async def status() -> JSONResponse:
        model_name = None
        tool_names: list[str] = []
        if agent is not None:
            model_name = getattr(agent._llm, "model", None)
            if agent._tools is not None:
                tool_names = [s.name for s in agent._tools.list_tools()]
        mcp_servers: list[str] = []
        if config is not None:
            mcp = config.get("mcp_servers") or {}
            if isinstance(mcp, dict):
                mcp_servers = list(mcp.keys())
        # B-142: surface MCP runtime state — connected/error/disabled
        # per server. Lets the UI show "the MCP servers you configured
        # are actually running" instead of just listing config keys.
        mcp_status: dict = {}
        _hub = getattr(app.state, "mcp_hub", None)
        if _hub is not None:
            try:
                mcp_status = _hub.status()
            except Exception:  # noqa: BLE001
                mcp_status = {}
        # Surface the daemon's currently-active workspace + total
        # registered roots so the topbar / chat-sidebar can show the
        # cwd context the agent is running against. Reads state.json
        # via WorkspaceManager so /api/v2/workspace mutations show
        # up here on the next call without a daemon restart.
        active_workspace: str | None = None
        workspace_count = 0
        try:
            from xmclaw.core.workspace import WorkspaceManager
            ws_state = WorkspaceManager().get()
            workspace_count = len(ws_state.roots)
            if ws_state.primary is not None:
                active_workspace = str(ws_state.primary.path)
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({
            "version": __version__,
            "agent_wired": agent is not None,
            "auth_required": auth_check is not None,
            "model": model_name,
            "tools": tool_names,
            "mcp_servers": mcp_servers,
            "mcp_status": mcp_status,  # B-142
            "sandbox_allowed_dirs": (
                [str(p) for p in (agent._tools._allowed or [])]
                if agent is not None and agent._tools is not None
                   and hasattr(agent._tools, "_allowed")
                else []
            ),
            "workspace": {
                "active": active_workspace,
                "count":  workspace_count,
            },
        })

    # ── /api/v2/llm/configure ─────────────────────────────────────
    # B-83: focused single-section LLM endpoint, twin of
    # /api/v2/memory/embedding/configure (B-76). The Web UI's
    # SetupBanner pops an inline form on missing.llm and POSTs here
    # rather than dropping the user into the generic Config page.
    @app.put("/api/v2/llm/configure")
    @app.post("/api/v2/llm/configure")
    async def configure_llm(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            return JSONResponse(
                {"ok": False, "error": "body must be a JSON object"},
                status_code=400,
            )
        provider = str(payload.get("provider", "")).strip().lower()
        if provider not in ("anthropic", "openai"):
            return JSONResponse(
                {"ok": False, "error": "provider must be 'anthropic' or 'openai'"},
                status_code=400,
            )
        api_key = str(payload.get("api_key", "")).strip()
        if not api_key:
            return JSONResponse(
                {"ok": False, "error": "api_key is required"}, status_code=400,
            )
        # base_url + default_model are optional — sane defaults if omitted.
        base_url = str(payload.get("base_url", "")).strip()
        default_model = str(payload.get("default_model", "")).strip()

        if config is None:
            return JSONResponse(
                {"ok": False, "error": "no config attached to daemon"},
                status_code=500,
            )

        block = config.setdefault("llm", {}).setdefault(provider, {})
        if not isinstance(block, dict):
            block = {}
            config["llm"][provider] = block
        block["api_key"] = api_key
        if base_url:
            block["base_url"] = base_url
        if default_model:
            block["default_model"] = default_model
        # Set this provider as default if none is set yet — first-config
        # case: the user just told us which one they have a key for, so
        # we shouldn't leave default_provider pointing at the empty other.
        llm_section = config["llm"]
        if not llm_section.get("default_provider"):
            llm_section["default_provider"] = provider

        if config_path:
            try:
                from xmclaw.utils.fs_locks import atomic_write_text
                p = Path(str(config_path))
                p.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(
                    p, json.dumps(config, indent=2, ensure_ascii=False),
                )
            except OSError as exc:
                return JSONResponse(
                    {"ok": False, "error": f"config write failed: {exc}"},
                    status_code=500,
                )
        return JSONResponse({
            "ok": True,
            "provider": provider,
            "default_provider": llm_section.get("default_provider"),
            "restart_required": True,
            "config_path": str(config_path) if config_path else None,
        })

    # ── /api/v2/setup ─────────────────────────────────────────────
    # B-81: aggregate "is this daemon ready for a new user yet?"
    # checklist used by the Web UI's SetupBanner. Each field is a
    # boolean that maps to one onboarding step; the front-end
    # constructs Chinese-language guidance from these flags.
    #
    # Distinct from /api/v2/status (which surfaces *current runtime*)
    # in that this endpoint answers "does the user need to do something
    # before XMclaw is useful?" — a question status was never designed
    # to answer.
    # B-102: run the full doctor pipeline programmatically + apply
    # fixes. Mirrors what ``xmclaw doctor [--fix] --json`` does on
    # the CLI but reachable from the Web UI's Doctor page so users
    # don't have to drop into a terminal to see check results.
    @app.post("/api/v2/doctor/run")
    async def doctor_run(payload: dict[str, Any] = None) -> JSONResponse:  # type: ignore[assignment]
        from xmclaw.cli.doctor_registry import (
            DoctorContext, build_default_registry,
        )
        body = payload or {}
        apply_fix = bool(body.get("fix", False))
        target_path = config_path or Path("daemon") / "config.json"
        ctx = DoctorContext(
            config_path=Path(target_path),
            host="127.0.0.1",
            port=8765,
            probe_daemon=False,  # avoid recursing into our own /health
        )
        reg = build_default_registry()
        results = reg.run_all(ctx)
        fixes_applied: list[str] = []
        if apply_fix:
            for check in reg.checks():
                # Re-run each fixable check after the initial sweep —
                # use the cached ctx.cfg from the first pass.
                try:
                    if check.fix(ctx):
                        fixes_applied.append(check.id)
                except Exception:  # noqa: BLE001 — fix must not crash run
                    pass
            # Re-run after fixes so the response shows the post-fix state.
            results = reg.run_all(ctx)
        return JSONResponse({
            "results": [
                {
                    "id": getattr(check, "id", ""),
                    "name": r.name,
                    "ok": r.ok,
                    "detail": r.detail,
                    "advisory": r.advisory,
                    "fix_available": r.fix_available,
                }
                for check, r in zip(reg.checks(), results)
            ],
            "summary": {
                "total": len(results),
                "ok": sum(1 for r in results if r.ok),
                "failed": sum(1 for r in results if not r.ok),
                "fixes_applied": fixes_applied,
            },
        })

    # B-99: surface in-flight ask_user_question calls so a browser
    # refresh can rebuild the QuestionCard. Without this, the user
    # who closed the tab mid-question has no way back — the daemon's
    # tool future is still ``await``-ing forever.
    @app.get("/api/v2/pending_questions")
    async def pending_questions() -> JSONResponse:
        try:
            from xmclaw.providers.tool.builtin import list_pending_questions
            items = list_pending_questions()
        except Exception:  # noqa: BLE001
            items = []
        return JSONResponse({"items": items})

    @app.get("/api/v2/setup")
    async def setup_status() -> JSONResponse:
        from xmclaw.daemon.factory import _resolve_persona_profile_dir

        cfg = config or {}

        # 1. LLM key configured? Walk both default-provider blocks AND
        # named profiles, since an Anthropic-only setup with the key
        # under llm.profiles[0] should still count.
        llm_section = cfg.get("llm") or {}
        llm_configured = False
        llm_provider_used: str | None = None
        for provider_name in ("anthropic", "openai"):
            block = llm_section.get(provider_name) or {}
            if isinstance(block, dict) and (block.get("api_key") or "").strip():
                llm_configured = True
                if llm_provider_used is None:
                    llm_provider_used = provider_name
        for prof in (llm_section.get("profiles") or []):
            if isinstance(prof, dict) and (prof.get("api_key") or "").strip():
                llm_configured = True
                if llm_provider_used is None:
                    llm_provider_used = str(prof.get("provider") or "?")

        # 2. Persona profile initialised? Bare-minimum SOUL.md or
        # IDENTITY.md present in the active profile dir tells us
        # `xmclaw onboard` (or its hand-written equivalent) has run.
        persona_ready = False
        try:
            pdir = _resolve_persona_profile_dir(cfg)
            if pdir.is_dir():
                for canon in ("SOUL.md", "IDENTITY.md"):
                    if (pdir / canon).is_file():
                        persona_ready = True
                        break
        except Exception:  # noqa: BLE001
            pass

        # 3. Embedding configured? Same key the indexer reads.
        emb_section = (
            ((cfg.get("evolution") or {}).get("memory") or {}).get("embedding")
        )
        embedding_configured = bool(
            isinstance(emb_section, dict)
            and (emb_section.get("model") or "").strip()
            and emb_section.get("dimensions")
        )

        # Indexer / dream cron actually running? Lifespan sets these
        # to non-None on success.
        indexer_obj = getattr(app.state, "memory_indexer", None)
        indexer_running = indexer_obj is not None
        dream_running = getattr(app.state, "dream_cron", None) is not None
        # B-87: precise reason the indexer isn't running, when applicable.
        # Lets the UI stop guessing "must be a missing restart" when
        # actually the embedder / vec_provider / start() failed.
        indexer_start_error = getattr(app.state, "indexer_start_error", None)
        # B-361 (Sprint 1): startup-time error capture (above) only
        # covers embedder/vec_provider/start() failures. The most
        # common production failure is "started cleanly but every
        # tick fails" — typically ``OperationalError('database is
        # locked')`` from PersonaStore.migrate / agent loop tools /
        # ExtractFactsHook all sharing the single sqlite connection.
        # Pre-B-361 the banner kept the start-time message and the
        # user followed the wrong fix (delete memory.db) and got
        # the same lock contention seconds later. Now we ALSO ask
        # the indexer for its tick-level health and override the
        # banner text with the actual root cause when ticks are
        # consistently failing.
        indexer_health: dict | None = None
        if indexer_obj is not None and hasattr(indexer_obj, "health_status"):
            try:
                indexer_health = indexer_obj.health_status()
            except Exception:  # noqa: BLE001 — observability never blocks
                indexer_health = None
        # If startup was clean but tick-loop is failing, surface the
        # truthful reason so the banner stops lying.
        if (
            indexer_start_error is None
            and indexer_health is not None
            and indexer_health.get("unhealthy_reason")
        ):
            reason = indexer_health["unhealthy_reason"]
            err = indexer_health.get("last_error", "")
            failures = indexer_health.get("consecutive_failures", 0)
            if reason == "db_locked":
                indexer_start_error = (
                    f"memory.db 多 task 写竞争（连续 {failures} 次 tick "
                    f"以 ``database is locked`` 失败）— 不是 sqlite_vec 未挂载，"
                    f"也不是 Ollama / 模型 / 维度问题。根因是 PersonaStore + "
                    f"indexer + agent 工具共享单一 sqlite connection 抢锁。"
                    f"B-362/B-363 永久修；临时缓解：xmclaw stop && xmclaw start "
                    f"后第一次刷新 memory 前等 30s。\n"
                    f"原始 error: {err}"
                )
            elif reason == "embed_failing":
                indexer_start_error = (
                    f"embedding 服务连续 {failures} 次 tick 失败。"
                    f"检查 Ollama / OpenAI / 自部署 endpoint 是否可达。\n"
                    f"原始 error: {err}"
                )
            elif reason == "unknown":
                indexer_start_error = (
                    f"indexer 启动 OK 但每次 tick 都失败（连续 {failures} 次）。"
                    f"原始 error: {err}"
                )

        missing: list[str] = []
        if not llm_configured:
            missing.append("llm")
        if not persona_ready:
            missing.append("persona")
        if not embedding_configured:
            missing.append("embedding")

        # B-368 (Sprint 1): MCP server health. daemon.log shows
        # ``mcp.start_failed name=filesystem err=npx not found`` × 109
        # over 2 weeks — UI showed nothing, user assumed daemon was
        # fine, the affected tool just silently disappeared from the
        # available list. Now expose per-server status so SetupBanner
        # can render a "MCP server X failed to start" item with a
        # concrete error string and a "what does this mean" tooltip.
        mcp_servers: dict | None = None
        mcp_hub = getattr(app.state, "mcp_hub", None)
        if mcp_hub is not None and hasattr(mcp_hub, "status"):
            try:
                mcp_servers = mcp_hub.status()
            except Exception:  # noqa: BLE001 — observability never blocks
                mcp_servers = None
        mcp_failed = [
            name for name, st in (mcp_servers or {}).items()
            if (st or {}).get("status") == "error"
        ]

        # B-350 (Sprint 1): expose the most recent CONFIG_RELOADED
        # summary so the UI can show a "config changed — restart" notice
        # for restart-bound sections. ``last_config_reload`` is None
        # when the daemon hasn't seen a reload since startup; once a
        # reload fires the watcher subscriber stashes the summary on
        # app.state.
        last_reload = getattr(app.state, "last_config_reload", None)

        return JSONResponse({
            "llm_configured": llm_configured,
            "llm_provider": llm_provider_used,
            "persona_ready": persona_ready,
            "embedding_configured": embedding_configured,
            "indexer_running": indexer_running,
            "indexer_start_error": indexer_start_error,
            "indexer_health": indexer_health,
            "dream_running": dream_running,
            "mcp_servers": mcp_servers,
            "mcp_failed": mcp_failed,
            "last_config_reload": last_reload,
            "missing": missing,
            "ready": len(missing) == 0,
        })

    # ── /api/v2/events — event-log replay / search (Epic #13) ────
    # When the bus is an SqliteEventBus, this endpoint exposes the
    # durable log: filter by session_id / since / until / types, or
    # do an FTS5 keyword search with q=. Falls back to the in-memory
    # session_logs buffer when the bus is not persistent (tests, CLI
    # echo mode), so clients can rely on a single endpoint shape.
    @app.get("/api/v2/events")
    async def events(
        session_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        types: str | None = None,   # comma-separated list of EventType values
        q: str | None = None,       # FTS5 keyword; takes precedence over range
        limit: int = 200,
        offset: int = 0,
    ) -> JSONResponse:
        # Clamp limit; the UI should paginate rather than request everything.
        limit = max(1, min(int(limit), 2000))
        offset = max(0, int(offset))

        type_list: list[EventType] = []
        if types:
            for raw in types.split(","):
                name = raw.strip()
                if not name:
                    continue
                try:
                    type_list.append(EventType(name))
                except ValueError:
                    continue  # silently drop unknown types

        results: list[BehavioralEvent] = []
        if isinstance(bus, SqliteEventBus):
            if q:
                results = bus.search(q, session_id=session_id, limit=limit)
            else:
                results = bus.query(
                    session_id=session_id,
                    since=since,
                    until=until,
                    types=type_list or None,
                    limit=limit,
                    offset=offset,
                )
        else:
            # In-memory fallback: filter the bounded session_logs buffer.
            source: list[BehavioralEvent]
            if session_id is not None:
                source = list(session_logs.get(session_id, []))
            else:
                source = [e for buf in session_logs.values() for e in buf]
            source.sort(key=lambda e: e.ts)
            for e in source:
                if since is not None and e.ts < since:
                    continue
                if until is not None and e.ts >= until:
                    continue
                if type_list and e.type not in type_list:
                    continue
                if q and q.lower() not in json.dumps(e.payload).lower():
                    continue
                results.append(e)
            results = results[offset : offset + limit]

        return JSONResponse({
            "events": [event_as_jsonable(e) for e in results],
            "count": len(results),
            "bus": type(bus).__name__,
        })

    # ── /ui/ static files + root redirect ──
    # Phase 4.6: serve a single-page UI bundled with the package, so
    # users can open `http://127.0.0.1:8765/` in a browser and get a
    # working chat interface. The UI files live in
    # xmclaw/daemon/static and are not auth-gated — the WebSocket
    # the UI connects to still requires the pairing token.
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        _index_html = _static_dir / "index.html"

        _static_root = _static_dir.resolve()

        # No-store + per-startup boot version. The bundle is plain ESM
        # served off disk (no build, no content-hashed filenames). Two
        # caches conspire against us:
        #
        # 1. Browser HTTP cache → fixed by ``Cache-Control: no-store``.
        # 2. Browser ESM module map (in-memory, scoped to the page
        #    lifetime) → no header can bust this. Only a *different
        #    URL* makes the browser treat the module as new. So we
        #    rewrite every relative ``import`` and ``<script src>`` to
        #    include ``?v=<BOOT_VERSION>``. BOOT_VERSION is the daemon
        #    startup timestamp — ``xmclaw stop && xmclaw start`` (or
        #    the in-UI 重启 button) is enough to force the entire
        #    module graph to refetch.
        import re as _re
        import time as _time
        BOOT_VERSION = str(int(_time.time()))

        _NO_STORE_HEADERS = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }

        # Match relative ESM specifiers used by ``import``,
        # ``import()`` and ``export ... from`` — both single + double
        # quoted. The regex deliberately doesn't touch absolute URLs
        # (https://esm.sh/preact, etc) or anything starting with /.
        _IMPORT_RE = _re.compile(
            r"""(\b(?:from|import)\s*\(?\s*)(["'])(\.{1,2}/[^"']+?)(["'])""",
            _re.MULTILINE,
        )
        # And a separate pattern for HTML ``<script src="./...">``.
        _HTML_SRC_RE = _re.compile(
            r"""(<script\b[^>]*\bsrc\s*=\s*)(["'])(\.{1,2}/[^"']+?)(["'])""",
            _re.MULTILINE,
        )

        def _stamp_url(specifier: str) -> str:
            """Append ?v=BOOT_VERSION (or &v= when query already present)."""
            sep = "&" if "?" in specifier else "?"
            return f"{specifier}{sep}v={BOOT_VERSION}"

        def _stamp_js(text: str) -> str:
            return _IMPORT_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}{_stamp_url(m.group(3))}{m.group(4)}",
                text,
            )

        def _stamp_html(text: str) -> str:
            return _HTML_SRC_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}{_stamp_url(m.group(3))}{m.group(4)}",
                text,
            )

        from starlette.responses import Response as _Response

        def _rewritten_response(path: Path) -> _Response | FileResponse:
            """Return either a rewritten Response (for .html / .js) or a
            plain FileResponse for everything else."""
            suffix = path.suffix.lower()
            if suffix == ".html":
                text = path.read_text(encoding="utf-8")
                return _Response(
                    _stamp_html(text),
                    media_type="text/html; charset=utf-8",
                    headers=_NO_STORE_HEADERS,
                )
            if suffix == ".js" or suffix == ".mjs":
                text = path.read_text(encoding="utf-8")
                return _Response(
                    _stamp_js(text),
                    media_type="application/javascript; charset=utf-8",
                    headers=_NO_STORE_HEADERS,
                )
            return FileResponse(str(path), headers=_NO_STORE_HEADERS)

        @app.get("/ui/{spa_path:path}", response_model=None)
        async def ui_spa_fallback(spa_path: str):
            if spa_path:
                candidate = (_static_dir / spa_path).resolve()
                try:
                    candidate.relative_to(_static_root)
                except ValueError:
                    return _rewritten_response(_index_html)
                if candidate.is_file():
                    return _rewritten_response(candidate)
            return _rewritten_response(_index_html)

        # StaticFiles is mounted as a fallback so paths the SPA route
        # above doesn't catch (rare; mostly directory-style URLs) still
        # resolve. We subclass to inject no-store + the same import
        # rewriting so the BOOT_VERSION reaches every served module.
        from starlette.responses import Response
        from starlette.types import Scope

        class _BootStampingStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope: Scope) -> Response:
                resp = await super().get_response(path, scope)
                for k, v in _NO_STORE_HEADERS.items():
                    resp.headers[k] = v
                return resp

        app.mount(
            "/ui",
            _BootStampingStaticFiles(directory=str(_static_dir), html=True),
            name="ui",
        )

        # B-MULTIMODAL-UI: serve screenshots saved by screen_capture /
        # screen_region_capture / image_read / camera_capture so the
        # chat UI can <img src="/api/v2/media/<filename>"> them.
        # Token-gated so private images can't be siphoned by another
        # tab on the same machine without the pairing token.
        from xmclaw.utils.paths import data_dir as _data_dir
        _media_dirs = [
            _data_dir() / "v2" / "screenshots",
            _data_dir() / "v2" / "audio",
            _data_dir() / "v2" / "uploads",  # user-uploaded media
        ]
        for _d in _media_dirs:
            _d.mkdir(parents=True, exist_ok=True)

        @app.get("/api/v2/media/{filename}", response_model=None)
        async def media_file(
            filename: str,
            token: str | None = None,
        ) -> FileResponse | JSONResponse:
            if auth_check is not None:
                try:
                    ok = await auth_check(token)
                except Exception:  # noqa: BLE001
                    ok = False
                if not ok:
                    return JSONResponse(
                        {"error": "unauthorized"}, status_code=401,
                    )
            # Defense-in-depth: filename basename only — no traversal.
            from pathlib import Path as _P
            safe = _P(filename).name
            if safe != filename or not safe:
                return JSONResponse(
                    {"error": "invalid filename"}, status_code=400,
                )
            for d in _media_dirs:
                p = d / safe
                if p.is_file():
                    # Common MIME types — let Starlette infer the
                    # rest from the suffix.
                    ext = p.suffix.lower()
                    mime = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".gif": "image/gif",
                        ".webp": "image/webp",
                        ".bmp": "image/bmp",
                        ".mp3": "audio/mpeg",
                        ".wav": "audio/wav",
                        ".ogg": "audio/ogg",
                        ".mp4": "video/mp4",
                        ".webm": "video/webm",
                    }.get(ext)
                    return FileResponse(
                        str(p),
                        media_type=mime,
                        headers=_NO_STORE_HEADERS,
                    )
            return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse(url="/ui/", status_code=302)

    @app.websocket("/agent/v2/{session_id}")
    async def agent_ws(ws: WebSocket, session_id: str) -> None:
        # B-355 (Sprint 1): Origin check. Defense-in-depth against
        # the OpenClaw "ClawJacked" CVE family — a malicious page on
        # http://evil.com running in the user's browser does
        # ``new WebSocket("ws://127.0.0.1:8765/agent/v2/foo")``.
        # Without the Origin check the daemon would happily upgrade
        # if the attacker can leak the pairing token (XSS in any
        # page user visits). With it, we reject the upgrade before
        # accept() if the Origin header doesn't match an allowed
        # origin. ``null`` Origin (file://, native apps, no-origin
        # WS) is allowed because browsers send it for legitimate
        # PWA / desktop tray tools. Loopback origins on any port
        # are always allowed.
        if not _origin_allowed(
            ws.headers.get("origin"), config or {},
        ):
            # WebSocket protocol: must accept() before close() to
            # send a code; bare reject closes with no client signal.
            await ws.accept()
            await ws.close(code=4403, reason="origin not allowed")
            return
        # Anti-req #8 gate. Token arrives either as a query param
        # (browsers can't set WS headers) or an Authorization: Bearer
        # header (CLIs / SDKs). We check both so we don't force one
        # choice on every kind of client.
        if auth_check is not None:
            token: str | None = ws.query_params.get("token")
            if not token:
                auth_header = ws.headers.get("authorization", "") or ""
                if auth_header.lower().startswith("bearer "):
                    token = auth_header[len("bearer "):].strip() or None
            ok = False
            try:
                ok = await auth_check(token)
            except Exception:  # noqa: BLE001 — auth must never crash daemon
                ok = False
            if not ok:
                # WebSocket protocol needs accept() before close(), or the
                # client gets a bare TCP reset with no close code. We want
                # 4401 visible to the client, so accept then close.
                await ws.accept()
                await ws.close(code=4401, reason="unauthorized")
                return

        # Epic #17 Phase 3: select which agent runs this session.
        # Clients omit ``agent_id`` (or send "main") for the primary
        # config-built agent; other values look up in the registry.
        # Unknown id closes the socket with 4404 — same pattern as
        # auth failure, so the client sees a structured error code
        # rather than a silent hang.
        requested_agent_id = ws.query_params.get("agent_id")
        active_agent: AgentLoop | None = agent
        resolved_agent_id = "main"
        if requested_agent_id and requested_agent_id != "main":
            ws_obj = agents_manager.get(requested_agent_id)
            if ws_obj is None or ws_obj.agent_loop is None:
                await ws.accept()
                await ws.close(code=4404, reason="agent not found")
                return
            active_agent = ws_obj.agent_loop
            resolved_agent_id = requested_agent_id

        await ws.accept()

        # B-348: if another tab is already on this session, supersede
        # it. We do this AFTER accept() because the socket needs to be
        # in OPEN state before we can send a frame or close cleanly.
        # The supersede frame lets the old tab's UI show a "你在另一
        # 个标签页打开了同一会话" notice instead of a bare disconnect.
        old_ws = active_ws_for_session.get(session_id)
        if old_ws is not None and old_ws is not ws:
            try:
                await old_ws.send_text(json.dumps({
                    "type": "superseded",
                    "payload": {
                        "session_id": session_id,
                        "reason": "another_tab_connected",
                    },
                    "session_id": session_id,
                }))
            except Exception:  # noqa: BLE001 — old socket may be dead
                pass
            try:
                # 4408 = "request timeout" in app-defined range; we
                # use it as "connection superseded by newer client".
                # Close happens after the supersede frame so the UI
                # has a chance to render the notice before the
                # transport tears down.
                await old_ws.close(code=4408, reason="superseded")
            except Exception:  # noqa: BLE001
                pass
        active_ws_for_session[session_id] = ws

        # ── replay historical events for this session ─────────
        # If the client is reconnecting to an existing session (browser
        # refresh), feed the prior events first so the chat div
        # repopulates. Each replayed frame carries ``replayed: true``
        # so the UI can suppress the thinking spinner and avoid
        # double-counting tokens.
        prior_events = list(session_logs.get(session_id, []))
        if prior_events:
            # Bracket the replay with marker frames so the client knows
            # when to enter / leave the "hydration" state.
            try:
                await ws.send_text(json.dumps({
                    "type": "session_replay", "payload": {
                        "phase": "start", "count": len(prior_events),
                    }, "session_id": session_id, "replayed": True,
                }))
                for event in prior_events:
                    await ws.send_text(json.dumps({
                        "id": event.id,
                        "ts": event.ts,
                        "session_id": event.session_id,
                        "agent_id": event.agent_id,
                        "type": event.type.value,
                        "payload": event.payload,
                        "correlation_id": event.correlation_id,
                        "parent_id": event.parent_id,
                        "schema_version": event.schema_version,
                        "replayed": True,
                    }))
                await ws.send_text(json.dumps({
                    "type": "session_replay", "payload": {"phase": "end"},
                    "session_id": session_id, "replayed": True,
                }))
            except Exception:  # noqa: BLE001
                pass

        # Subscribe this connection to the bus BEFORE the lifecycle event
        # so the client sees its own session-create frame.
        outbox: list[BehavioralEvent] = []

        # Evolution events are globally interesting: a promotion moves
        # HEAD for *everyone*, so every connected REPL should see the
        # flash regardless of which session triggered the mutation.
        # The orchestrator emits them with session_id="_system" by
        # default, so without this carve-out they'd be silently filtered
        # out by the per-session forwarder.
        _GLOBAL_EVENT_TYPES = frozenset({
            EventType.SKILL_PROMOTED,
            EventType.SKILL_ROLLED_BACK,
            EventType.SKILL_CANDIDATE_PROPOSED,
        })

        def _is_relevant(event: BehavioralEvent) -> bool:
            return (
                event.session_id == session_id
                or event.type in _GLOBAL_EVENT_TYPES
            )

        async def forward(event: BehavioralEvent) -> None:
            # Per-session events + globally interesting events (promotions,
            # rollbacks, candidate proposals). Everything else is filtered
            # out to avoid leaking private conversations across sockets.
            if not _is_relevant(event):
                return
            outbox.append(event)
            try:
                await ws.send_text(json.dumps({
                    "id": event.id,
                    "ts": event.ts,
                    "session_id": event.session_id,
                    "agent_id": event.agent_id,
                    "type": event.type.value,
                    "payload": event.payload,
                    "correlation_id": event.correlation_id,
                    "parent_id": event.parent_id,
                    "schema_version": event.schema_version,
                }))
            except Exception:  # noqa: BLE001 — socket might close mid-send
                pass

        sub = bus.subscribe(
            _is_relevant,
            forward,
        )

        # Announce the session.
        await bus.publish(make_event(
            session_id=session_id, agent_id="daemon",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create", "via": "ws"},
        ))
        await bus.drain()

        try:
            while True:
                try:
                    raw = await ws.receive_text()
                except RuntimeError as exc:
                    # B-23: client disconnected before the server's
                    # accept() handshake fully completed (rare race
                    # under heavy test load). Starlette raises
                    # ``RuntimeError("WebSocket is not connected. Need
                    # to call "accept" first.")`` — log nothing, exit
                    # the loop the same way as a clean disconnect.
                    if "not connected" in str(exc).lower():
                        break
                    raise
                try:
                    frame: Any = json.loads(raw)
                except json.JSONDecodeError:
                    # Drop malformed frames; connection stays open.
                    continue
                if not isinstance(frame, dict):
                    continue
                # Frame shape: {"type": "user", "content": "...",
                #                "ultrathink": bool?}
                if frame.get("type") == "user":
                    content = str(frame.get("content", ""))
                    ultrathink = bool(frame.get("ultrathink", False))
                    user_corr = frame.get("correlation_id")
                    if user_corr is not None and not isinstance(user_corr, str):
                        user_corr = None
                    # Multi-model: client picks which configured profile
                    # to route this turn through. Unset → AgentLoop uses
                    # the registry default (legacy single-LLM block).
                    llm_profile_id = frame.get("llm_profile_id")
                    if llm_profile_id is not None and not isinstance(llm_profile_id, str):
                        llm_profile_id = None
                    # B-MULTIMODAL-UI: composer can attach images / video /
                    # audio. Each entry is a data: URL the browser
                    # built via FileReader.readAsDataURL. Persist each
                    # to disk under ~/.xmclaw/v2/uploads/ and pass the
                    # ABSOLUTE PATH to run_turn — agent_loop then sets
                    # Message.images so the LLM translator encodes
                    # them as vision content blocks.
                    user_image_paths: list[str] = []
                    raw_images = frame.get("images")
                    if isinstance(raw_images, list):
                        from xmclaw.utils.paths import data_dir as _data_dir
                        import base64 as _b64
                        import uuid as _uuid
                        from pathlib import Path as _P
                        uploads_dir = (
                            _data_dir() / "v2" / "uploads"
                        )
                        uploads_dir.mkdir(parents=True, exist_ok=True)
                        for entry in raw_images[:8]:
                            if not isinstance(entry, str):
                                continue
                            if not entry.startswith("data:"):
                                continue
                            try:
                                header, payload_b64 = entry.split(",", 1)
                                # e.g. "data:image/png;base64"
                                meta = header[len("data:"):]
                                mime = (meta.split(";")[0] or "image/png").lower()
                                ext_map = {
                                    "image/png": ".png",
                                    "image/jpeg": ".jpg",
                                    "image/jpg": ".jpg",
                                    "image/gif": ".gif",
                                    "image/webp": ".webp",
                                    "image/bmp": ".bmp",
                                    "video/mp4": ".mp4",
                                    "video/webm": ".webm",
                                    "audio/wav": ".wav",
                                    "audio/mpeg": ".mp3",
                                    "audio/ogg": ".ogg",
                                }
                                ext = ext_map.get(mime, ".bin")
                                raw_bytes = _b64.b64decode(payload_b64)
                                if len(raw_bytes) > 8 * 1024 * 1024:
                                    continue  # reject huge uploads
                                out = uploads_dir / f"{int(time.time())}_{_uuid.uuid4().hex[:8]}{ext}"
                                out.write_bytes(raw_bytes)
                                user_image_paths.append(str(out))
                            except Exception:  # noqa: BLE001 — skip bad blobs
                                continue
                    # Ultrathink (borrowed from the /ultrathink pattern):
                    # when set, prepend a directive to make the model
                    # slow down and think step-by-step before answering.
                    # Works on any chat model -- we don't need provider
                    # support for extended-thinking parameters.
                    if ultrathink:
                        content = (
                            "Before answering, think step-by-step. "
                            "Enumerate the subproblems, consider alternatives, "
                            "and only then give your final answer.\n\n"
                            f"User: {content}"
                        )
                    if active_agent is not None:
                        # Phase 4.1: run the full LLM ↔ tool loop. The
                        # AgentLoop publishes USER_MESSAGE + every LLM /
                        # tool event onto the bus; our subscription
                        # forwards them to this WS. Epic #17 Phase 4:
                        # wrap in ``use_current_agent_id`` so tools
                        # invoked during the turn (e.g., agent-to-agent)
                        # can discover which agent initiated them.
                        try:
                            with use_current_agent_id(resolved_agent_id):
                                await active_agent.run_turn(
                                    session_id, content,
                                    user_correlation_id=user_corr,
                                    llm_profile_id=llm_profile_id,
                                    user_images=(
                                        tuple(user_image_paths)
                                        if user_image_paths else None
                                    ),
                                )
                        except Exception as exc:  # noqa: BLE001
                            # Surface a structured error frame so the
                            # client sees the failure instead of a
                            # silent socket stall.
                            await bus.publish(make_event(
                                session_id=session_id, agent_id="daemon",
                                type=EventType.ANTI_REQ_VIOLATION,
                                payload={
                                    "message": f"agent loop crashed: {type(exc).__name__}: {exc}",
                                },
                            ))
                            await bus.drain()
                    else:
                        # Phase 4.0 fallback: plain bus-echo for tests
                        # and for clients that do their own reasoning.
                        await bus.publish(make_event(
                            session_id=session_id, agent_id="daemon",
                            type=EventType.USER_MESSAGE,
                            payload={
                                "content": content,
                                "channel": "ws",
                            },
                            correlation_id=user_corr,
                        ))
                        await bus.drain()
                # B-38: handle cancel frame — Stop button in Chat
                # sends ``{"type": "cancel"}`` while a turn is in
                # flight. Sets the AgentLoop's per-session event so
                # the run_turn hop loop bails at the next boundary.
                # No-op when no turn is running.
                elif frame.get("type") == "cancel":
                    if active_agent is not None:
                        try:
                            cancelled = active_agent.cancel_session(session_id)
                        except Exception:  # noqa: BLE001
                            cancelled = False
                        await bus.publish(make_event(
                            session_id=session_id, agent_id="daemon",
                            type=EventType.SESSION_LIFECYCLE,
                            payload={
                                "phase": "cancel_requested",
                                "active": cancelled,
                            },
                        ))
                        await bus.drain()
                # B-106: undo frame — drop the last user/assistant pair
                # from history. Used by /undo slash command. Echoes a
                # SESSION_LIFECYCLE event back so the UI can flush the
                # last bubble and refresh from the new history.
                elif frame.get("type") == "undo":
                    removed = 0
                    history_len = 0
                    if active_agent is not None:
                        try:
                            res = active_agent.pop_last_turn(session_id)
                            removed = int(res.get("removed", 0))
                            history_len = int(res.get("history_len", 0))
                        except Exception:  # noqa: BLE001
                            removed = 0
                    await bus.publish(make_event(
                        session_id=session_id, agent_id="daemon",
                        type=EventType.SESSION_LIFECYCLE,
                        payload={
                            "phase": "undo_applied",
                            "removed": removed,
                            "history_len": history_len,
                        },
                    ))
                    await bus.drain()
                # B-92: handle answer_question frame — UI's QuestionCard
                # sends ``{"type": "answer_question", "question_id": "...",
                # "value": "..."}`` when the user clicks an option.
                # Resolves the in-flight Future inside the
                # ask_user_question tool handler so the agent's tool
                # invocation unblocks and the run_turn loop continues.
                # ``value`` is a string (single-select / Other) or a
                # list of strings (multi-select).
                elif frame.get("type") == "answer_question":
                    qid = frame.get("question_id")
                    value = frame.get("value")
                    resolved = False
                    if isinstance(qid, str) and qid and value is not None:
                        try:
                            from xmclaw.providers.tool.builtin import (
                                resolve_pending_question,
                            )
                            resolved = resolve_pending_question(qid, value)
                        except Exception:  # noqa: BLE001
                            resolved = False
                    # Re-broadcast the answer so the chat transcript +
                    # event log replay can show what the user picked.
                    # Always publish (even on stale answer) so the UI
                    # has something to clear the QuestionCard with.
                    await bus.publish(make_event(
                        session_id=session_id, agent_id="user",
                        type=EventType.USER_ANSWERED_QUESTION,
                        payload={
                            "question_id": qid or "",
                            "value": value,
                            "resolved": resolved,
                        },
                    ))
                    await bus.drain()
                # Other frame types are silently ignored for now.
        except WebSocketDisconnect:
            pass
        finally:
            sub.cancel()
            # B-348: only deregister if WE are still the registered
            # active WS for this session. If a newer tab already
            # superseded us, the dict already points at that tab —
            # popping would clobber the new owner's registration.
            if active_ws_for_session.get(session_id) is ws:
                del active_ws_for_session[session_id]
            # Do NOT wipe session history on disconnect -- browser refresh
            # is a WS close, and the user's prior exchanges must survive
            # it. History stays in the AgentLoop's in-memory dict keyed
            # by session_id. Explicit reset is via agent.clear_session()
            # which the UI triggers with a /reset intent (not on close).
            #
            # Bounded by AgentLoop.history_cap (default 40 messages per
            # session). Sessions created and then never reconnected do
            # leak until the daemon restarts -- acceptable for now since
            # sessions are user-created and finite. Cross-process
            # persistence (SQLite-backed session store) lands later.
            await bus.publish(make_event(
                session_id=session_id, agent_id="daemon",
                type=EventType.SESSION_LIFECYCLE,
                payload={"phase": "destroy", "via": "ws"},
            ))
            await bus.drain()

            # Cross-session memory hook: if this session had real
            # back-and-forth (>= 4 exchanges = 8 messages incl. agent
            # replies), schedule a delayed reflection turn that asks
            # the agent to write any durable insights to MEMORY.md or
            # USER.md. Keeps the user's "what did we figure out" from
            # evaporating between conversations — directly addresses
            # the "记忆依赖文件" / "跨会话记忆关联" gap the user
            # called out in B-15.
            try:
                tgt_agent = getattr(app.state, "agent", None)
                if tgt_agent is not None:
                    history = tgt_agent._histories.get(session_id, [])  # noqa: SLF001
                    msg_count = len(history)
                    # 8 = roughly 4 user-assistant exchanges. Below
                    # that, reflection isn't worth the LLM call.
                    if msg_count >= 8:
                        # Spawn the reflection in the background so the
                        # WS close path returns immediately. Failures
                        # are logged but don't propagate.
                        import asyncio as _asyncio
                        # B-70: hold ref to prevent mid-flight GC.
                        _refl = _asyncio.create_task(
                            _run_session_reflection(
                                tgt_agent, session_id, msg_count,
                            ),
                            name=f"xmclaw-reflect-{session_id}",
                        )
                        _PENDING_REFLECTIONS.add(_refl)
                        _refl.add_done_callback(_PENDING_REFLECTIONS.discard)
            except Exception:  # noqa: BLE001
                pass

    return app


# Convenience: a default app instance for `uvicorn xmclaw.daemon.app:app`.
app = create_app()