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

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

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


_SECRET_KEYS = frozenset({
    "api_key", "apikey", "bot_token", "app_token", "token",
    "password", "secret", "authorization",
})


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
        except Exception:  # noqa: BLE001 — malformed memory config must not block daemon
            memory = None
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

    # Epic #17 Phase 3: multi-agent registry. Constructed eagerly so the
    # routers and WS handler can rely on ``app.state.agents`` being set,
    # but rehydration from disk happens in lifespan so tests that never
    # enter lifespan don't pay the filesystem walk.
    agents_manager = MultiAgentManager(bus)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if sweep_task is not None:
            await sweep_task.start()
        if backup_scheduler is not None:
            await backup_scheduler.start()
        try:
            await agents_manager.load_from_disk()
        except Exception:  # noqa: BLE001 — bad preset file must not block boot
            pass
        # Epic #4 Phase C: start the EvolutionOrchestrator so auto_apply
        # subscriptions go live. No-op when orchestrator is None or
        # auto_apply is False (it still publishes events on explicit
        # promote/rollback, just doesn't consume proposals). Failures
        # here must not prevent the daemon from serving WS traffic —
        # evolution is a best-effort observability layer, not a
        # critical path.
        if orchestrator is not None:
            try:
                await orchestrator.start()
            except Exception:  # noqa: BLE001
                pass
        try:
            yield
        finally:
            if sweep_task is not None:
                await sweep_task.stop()
            if backup_scheduler is not None:
                await backup_scheduler.stop()
            # Epic #17 Phase 7: stop all workspace background work
            # before tearing down the bus + memory store. Evolution
            # observers cancel their subscriptions here; LLM workspaces
            # are a no-op.
            for _ws_id in agents_manager.list_ids():
                _ws = agents_manager.get(_ws_id)
                if _ws is None:
                    continue
                try:
                    await _ws.stop()
                except Exception:  # noqa: BLE001 — one bad stop must not abort shutdown
                    pass
            if orchestrator is not None:
                try:
                    await orchestrator.stop()
                except Exception:  # noqa: BLE001
                    pass
            if memory is not None and hasattr(memory, "close"):
                try:
                    memory.close()
                except Exception:  # noqa: BLE001
                    pass

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

    # Phase 3: ASGI middleware for X-Agent-Id → ContextVar plumbing
    # (QwenPaw multi-agent convention #1). Stays a no-op for the
    # default "main" agent id, so existing single-agent flows aren't
    # affected.
    from xmclaw.daemon.middleware import AgentScopeMiddleware
    app.add_middleware(AgentScopeMiddleware)

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
            config, bus, approval_service=app.state.approval_service
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
        _inter = AgentInterTools(manager=agents_manager, primary_loop=agent)
        if agent._tools is None:
            agent._tools = _inter
        else:
            agent._tools = CompositeToolProvider(agent._tools, _inter)

    app.state.agent = agent
    # Expose the multi-model registry so routers/llm_profiles.py can
    # enumerate live profiles without reaching into AgentLoop internals.
    if agent is not None:
        app.state.llm_registry = getattr(agent, "_llm_registry", None)

    # ── per-session event log (for reconnect replay) ─────────────
    # When a browser refresh disconnects and reconnects to the same
    # session_id, the client has an empty chat div -- live events
    # alone can't repopulate the transcript. So we tap the bus with a
    # global subscriber and keep a bounded log per session_id. On WS
    # connect, we stream the log first, then go live.
    _SESSION_LOG_CAP = 400  # events per session; ~20 turns of back-and-forth
    session_logs: dict[str, list[BehavioralEvent]] = {}

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

        # SPA fallback: client-side router owns /ui/chat, /ui/agents, …
        # so a refresh on those paths must return index.html, not 404.
        # Real static assets (.js / .css / .png / fonts) are served by
        # the StaticFiles mount below; anything else falls through here.
        _static_root = _static_dir.resolve()

        @app.get("/ui/{spa_path:path}", response_model=None)
        async def ui_spa_fallback(spa_path: str) -> FileResponse:
            if spa_path:
                candidate = (_static_dir / spa_path).resolve()
                # Reject `..` traversal: candidate must live under static root.
                try:
                    candidate.relative_to(_static_root)
                except ValueError:
                    return FileResponse(str(_index_html))
                if candidate.is_file():
                    return FileResponse(str(candidate))
            return FileResponse(str(_index_html))

        app.mount(
            "/ui",
            StaticFiles(directory=str(_static_dir), html=True),
            name="ui",
        )

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse(url="/ui/", status_code=302)

    @app.websocket("/agent/v2/{session_id}")
    async def agent_ws(ws: WebSocket, session_id: str) -> None:
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
                raw = await ws.receive_text()
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
                # Other frame types are silently ignored for now.
                # Phase 4.2+ will add cancel / ask_user_answer / etc.
        except WebSocketDisconnect:
            pass
        finally:
            sub.cancel()
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

    return app


# Convenience: a default app instance for `uvicorn xmclaw.daemon.app:app`.
app = create_app()
