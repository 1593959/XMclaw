"""Cognition API — 认知架构状态与操作。

Mounted at ``/api/v2/cognition``. 当 cognition.enabled=false 或
启动失败时，所有端点返回 503 并附带降级信息。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/cognition", tags=["cognition"])


def _state(request: Request) -> Any:
    """Return the live app.state (None when daemon not booted)."""
    return getattr(request.app, "state", None)


def _cognitive_state(request: Request) -> Any | None:
    """Return the agent's CognitiveState if wired.

    Phase 5: in multi-agent mode the shared state lives on
    ``app.state.cognitive_state``; single-agent mode falls back to
    ``agent._cognitive_state``.
    """
    st = _state(request)
    if st is not None:
        shared = getattr(st, "cognitive_state", None)
        if shared is not None:
            return shared
    agent = getattr(st, "agent", None) if st is not None else None
    if agent is None:
        return None
    return getattr(agent, "_cognitive_state", None)


def _task_scheduler(request: Request) -> Any | None:
    return getattr(_state(request), "task_scheduler", None)


def _evolution_loop(request: Request) -> Any | None:
    return getattr(_state(request), "evolution_loop", None)


def _memory_graph(request: Request) -> Any | None:
    return getattr(_state(request), "memory_graph", None)


def _not_wired(request: Request | None = None) -> JSONResponse:
    """503 with structured ``reason`` so the UI can render an
    actionable "how to enable" panel rather than a bare 503.

    Reason taxonomy:
      * ``disabled``      — config has cognition.enabled=false (default).
      * ``failed_startup``— cognition.enabled=true but lifespan caught an
                            exception while constructing CognitiveState
                            / MemoryGraph / TaskScheduler.
      * ``missing_dep``   — placeholder for future dep-missing path
                            (currently unused; reserved).

    The UI keys off ``reason`` to show the right remediation copy.
    """
    reason = "disabled"
    hint = (
        "Set ``cognition.enabled = true`` in daemon/config.json and "
        "restart the daemon (xmclaw stop && xmclaw start)."
    )
    if request is not None:
        cfg = getattr(request.app.state, "config", None) or {}
        cognition_cfg = (cfg.get("cognition") or {}) if isinstance(cfg, dict) else {}
        if cognition_cfg.get("enabled"):
            # Config says yes but state is None → lifespan failed.
            reason = "failed_startup"
            hint = (
                "cognition.enabled=true but the daemon failed to "
                "construct the cognitive substrate. Check "
                "~/.xmclaw/v2/logs/xmclaw.log for "
                "``cognition.state_load_failed`` / ``cognition."
                "file_watcher_start_failed`` / ``cognition."
                "evolution_loop_start_failed`` warnings."
            )
    return JSONResponse(
        {
            "error": "cognition not enabled or failed to start",
            "reason": reason,
            "hint": hint,
            "how_to_enable": [
                "Open daemon/config.json (path: ~/.xmclaw/v2/ or ./daemon/)",
                "Set: { \"cognition\": { \"enabled\": true } }",
                "Optional Phase 6: { \"cognition\": { \"continuous_loop\": { \"enabled\": true, \"autonomy_level\": 0 } } }",
                "Save → xmclaw stop && xmclaw start",
                "See docs/JARVIS_PHASE_6_DESIGN.md §4 for autonomy levels 0/50/100",
            ],
        },
        status_code=503,
    )


# ── cognitive state ───────────────────────────────────────────────


@router.get("/state")
async def get_state(request: Request) -> JSONResponse:
    """Dump the live CognitiveState."""
    cs = _cognitive_state(request)
    if cs is None:
        return _not_wired(request)
    return JSONResponse({
        "goals": [
            {
                "id": g.id,
                "description": g.description,
                "priority": g.priority,
                "source": g.source,
                "status": g.status,
            }
            for g in cs.current_goals
        ],
        "attention_focus": [
            {
                "percept_id": f.percept_id,
                "content": f.content,
                "salience_score": round(f.salience_score, 3),
            }
            for f in cs.attention_focus
        ],
        "fatigue": {
            k: round(v, 2)
            for k, v in cs.fatigue.items()
        },
        "salience_threshold": cs.salience_threshold,
        "attention_capacity": cs.attention_capacity,
    })


@router.post("/goals")
async def add_goal(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Add a goal to the cognitive state.

    R2 (2026-05-10) extended: payload may now carry the new Goal
    fields (success_criteria / deadline / assigned_agent /
    estimated_cost_usd). All optional with backward-compat defaults.
    """
    cs = _cognitive_state(request)
    if cs is None:
        return _not_wired(request)
    description = str(payload.get("description", "")).strip()
    if not description:
        return JSONResponse({"error": "description required"}, status_code=400)
    from xmclaw.cognition.state import Goal
    import uuid as _uuid
    raw_id = payload.get("id")
    goal_id = (str(raw_id).strip() if raw_id else "") or _uuid.uuid4().hex
    goal = Goal(
        id=goal_id,
        description=description,
        priority=int(payload.get("priority", 5)),
        source=str(payload.get("source", "user")),
        success_criteria=(
            str(payload["success_criteria"]).strip()
            if payload.get("success_criteria") else None
        ),
        deadline=(
            float(payload["deadline"])
            if payload.get("deadline") is not None else None
        ),
        assigned_agent=str(payload.get("assigned_agent", "main")),
    )
    cs.add_goal(goal)
    return JSONResponse({
        "ok": True,
        "goal": {
            "id": goal.id,
            "description": goal.description,
            "priority": goal.priority,
            "success_criteria": goal.success_criteria,
            "assigned_agent": goal.assigned_agent,
        },
    })


# ── R2: HTN plan + materialize endpoint ──────────────────────────


@router.post("/goals/plan")
async def plan_goal(
    request: Request, payload: dict[str, Any],
) -> JSONResponse:
    """HTN-decompose a goal into a Task DAG and (optionally) submit
    it to the TaskScheduler.

    Body:
        {
          "description": str,           # required
          "success_criteria": str?,
          "priority": int? (1-10, default 5),
          "materialize": bool? (default false)
                                        # true → submit Tasks to scheduler
                                        # false → return plan tree only
                                        #         (preview / dry-run mode)
          "max_depth": int? (default 3),
          "max_total_cost_usd": float? (default 1.0),
        }

    Returns:
        {
          "plan": <BoundGoal tree as nested dict>,
          "leaves": [...]      # flat list of atomic leaves
          "estimated_cost_usd": float,
          "task_ids": [...]    # populated only when materialize=true
        }
    """
    description = str(payload.get("description", "")).strip()
    if not description:
        return JSONResponse(
            {"error": "description required"}, status_code=400,
        )

    # Find the agent's LLM — HTNPlanner needs one and we don't want
    # to spin a separate one. Pre-2026-05-10 callers without an agent
    # see a friendly 503.
    agent = getattr(request.app.state, "agent", None)
    llm = getattr(agent, "_llm", None) if agent else None
    if llm is None:
        return JSONResponse({
            "error": "no_llm_wired",
            "hint": "/cognition/goals/plan needs an agent.LLM; "
                    "ensure llm is configured in daemon/config.json",
        }, status_code=503)

    from xmclaw.cognition.htn_planner import HTNPlanner

    planner = HTNPlanner(
        llm=llm,
        max_depth=int(payload.get("max_depth", 3)),
        max_sub_goals=int(payload.get("max_sub_goals", 6)),
        max_total_cost_usd=float(
            payload.get("max_total_cost_usd", 1.0),
        ),
    )

    from xmclaw.cognition.state import Goal
    import uuid as _uuid
    raw_id = payload.get("id")
    goal_id = (str(raw_id).strip() if raw_id else "") or _uuid.uuid4().hex
    goal = Goal(
        id=goal_id,
        description=description,
        priority=max(1, min(10, int(payload.get("priority", 5)))),
        success_criteria=(
            str(payload["success_criteria"]).strip()
            if payload.get("success_criteria") else None
        ),
    )

    bound = await planner.plan(goal)

    # Optionally submit to the scheduler.
    task_ids: list[str] = []
    materialize_flag = bool(payload.get("materialize", False))
    if materialize_flag:
        scheduler = getattr(request.app.state, "task_scheduler", None)
        if scheduler is None:
            return JSONResponse({
                "error": "no_scheduler_wired",
                "hint": "task_scheduler not in app.state — "
                        "cognition.continuous_loop.enabled must be true",
                "plan": _bound_to_dict(bound),
            }, status_code=503)
        task_ids = await planner.materialize(bound, scheduler=scheduler)

    return JSONResponse({
        "plan": _bound_to_dict(bound),
        "leaves": [_bound_to_dict(leaf) for leaf in bound.atomic_leaves()],
        "estimated_cost_usd": round(
            bound.total_estimated_cost_usd(), 4,
        ),
        "task_ids": task_ids,
    })


def _bound_to_dict(b: Any) -> dict[str, Any]:
    """Render a BoundGoal tree as JSON-friendly nested dict."""
    out = {
        "goal_id": b.goal_id,
        "description": b.description,
        "success_criteria": b.success_criteria,
        "priority": b.priority,
        "kind": b.kind,
        "depth": b.depth,
    }
    if b.kind == "atomic":
        out["task_prompt"] = b.task_prompt
        out["estimated_cost_usd"] = b.estimated_cost_usd
        if b.error:
            out["error"] = b.error
    else:
        out["children"] = [_bound_to_dict(c) for c in b.children]
        out["edges"] = [list(e) for e in b.edges]
    return out


@router.delete("/goals/{goal_id}")
async def complete_goal(request: Request, goal_id: str) -> JSONResponse:
    """Mark a goal as completed."""
    cs = _cognitive_state(request)
    if cs is None:
        return _not_wired(request)
    ok = cs.complete_goal(goal_id)
    return JSONResponse({"ok": ok})


# ── tasks ─────────────────────────────────────────────────────────


@router.get("/tasks")
async def list_tasks(request: Request) -> JSONResponse:
    """List tasks from the TaskScheduler."""
    sched = _task_scheduler(request)
    if sched is None:
        return _not_wired(request)
    status = request.query_params.get("status")
    tasks = await sched.list_tasks(status=status, limit=100)
    return JSONResponse({
        "tasks": [t.to_dict() for t in tasks],
    })


@router.get("/tasks/graph-state")
async def task_graph_state(request: Request) -> JSONResponse:
    """Return the canonical GraphState snapshot for scheduled tasks."""
    sched = _task_scheduler(request)
    if sched is None:
        return _not_wired(request)
    snapshot = getattr(sched, "snapshot_graph_state", None)
    if snapshot is None:
        return JSONResponse(
            {"ok": False, "error": "task scheduler does not support graph_state"},
            status_code=503,
        )
    state = await snapshot(
        thread_id="cognition-api",
        run_id="task-scheduler-api",
        goal="task scheduler graph",
    )
    return JSONResponse(state.snapshot())


@router.get("/tasks/graph")
async def task_graph(request: Request) -> JSONResponse:
    """Return task dependency graph (DAG) for visualisation.

    NB: this MUST be registered BEFORE ``/tasks/{task_id}`` because
    FastAPI matches routes in registration order — otherwise a GET
    to ``/tasks/graph`` would match ``/tasks/{task_id}`` with
    task_id="graph" and (correctly) 404.
    """
    sched = _task_scheduler(request)
    if sched is None:
        return _not_wired(request)
    tasks = await sched.list_tasks(limit=200)
    nodes = [
        {
            "id": t.id,
            "label": t.prompt[:40] + "…" if len(t.prompt) > 40 else t.prompt,
            "status": t.status,
            "priority": t.priority,
        }
        for t in tasks
    ]
    edges = []
    for t in tasks:
        for dep in t.dependencies:
            edges.append({"source": dep, "target": t.id})
    return JSONResponse({"nodes": nodes, "edges": edges})


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> JSONResponse:
    """Get a single task + progress."""
    sched = _task_scheduler(request)
    if sched is None:
        return _not_wired(request)
    task = await sched.get_task(task_id)
    if task is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    progress = await sched.get_progress(task_id)
    return JSONResponse({"task": task.to_dict(), "progress": progress})


@router.post("/tasks")
async def submit_task(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Submit a new task."""
    sched = _task_scheduler(request)
    if sched is None:
        return _not_wired(request)
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)
    from xmclaw.cognition.task_scheduler import Task
    task = Task(
        id=payload.get("id", ""),
        prompt=prompt,
        priority=int(payload.get("priority", 5)),
        dependencies=list(payload.get("dependencies", [])),
        max_retries=int(payload.get("max_retries", 3)),
        timeout_seconds=int(payload.get("timeout_seconds", 300)),
    )
    tid = await sched.submit(task)
    return JSONResponse({"ok": True, "task_id": tid})


@router.delete("/tasks/{task_id}")
async def cancel_task(request: Request, task_id: str) -> JSONResponse:
    """Cancel a task."""
    sched = _task_scheduler(request)
    if sched is None:
        return _not_wired(request)
    ok = await sched.cancel(task_id)
    return JSONResponse({"ok": ok})


# ── proposals (evolution) ─────────────────────────────────────────


@router.get("/proposals")
async def list_proposals(request: Request) -> JSONResponse:
    """List pending evolution proposals."""
    evo = _evolution_loop(request)
    if evo is None:
        return _not_wired(request)
    proposals = await evo.list_pending()
    return JSONResponse({
        "proposals": [
            {
                "id": p.id,
                "type": p.type,
                "description": p.description,
                "target": p.target,
                "confidence": round(p.confidence, 3),
                "status": p.status,
                "created_at": p.created_at,
            }
            for p in proposals
        ],
    })


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(request: Request, proposal_id: str) -> JSONResponse:
    """Approve an evolution proposal."""
    evo = _evolution_loop(request)
    if evo is None:
        return _not_wired(request)
    ok = await evo.approve(proposal_id)
    return JSONResponse({"ok": ok})


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(request: Request, proposal_id: str) -> JSONResponse:
    """Reject an evolution proposal."""
    evo = _evolution_loop(request)
    if evo is None:
        return _not_wired(request)
    ok = await evo.reject(proposal_id)
    return JSONResponse({"ok": ok})


@router.post("/proposals/auto_approve_pending")
async def auto_approve_pending_proposals(request: Request) -> JSONResponse:
    """Wave-32+ backfill: sweep the entire pending pile and auto-
    approve everything that clears the configured confidence
    threshold. Use when you've just enabled the feature and want to
    clear an existing backlog without waiting for the next
    evolution cycle.

    Returns ``{ok, approved, kept_pending, skipped_errors}``.
    """
    evo = _evolution_loop(request)
    if evo is None:
        return _not_wired(request)
    counts = await evo.auto_approve_pending()
    return JSONResponse({"ok": True, **counts})


# ── memory graph ──────────────────────────────────────────────────


@router.get("/graph/stats")
async def graph_stats(request: Request) -> JSONResponse:
    """Return MemoryGraph statistics."""
    graph = _memory_graph(request)
    if graph is None:
        return _not_wired(request)
    stats = await graph.stats()
    return JSONResponse(stats)


@router.get("/graph/nodes")
async def graph_nodes(request: Request) -> JSONResponse:
    """Query graph nodes by type."""
    graph = _memory_graph(request)
    if graph is None:
        return _not_wired(request)
    node_type = request.query_params.get("type", "event")
    limit = int(request.query_params.get("limit", 10))
    nodes = await graph.query_by_type(node_type, limit=limit)  # type: ignore[arg-type]
    return JSONResponse({
        "nodes": [
            {
                "id": n.id,
                "type": n.type,
                "content": n.content[:200],
                "created_at": n.created_at,
            }
            for n in nodes
        ],
    })


@router.get("/graph/neighbors/{node_id}")
async def graph_neighbors(request: Request, node_id: str) -> JSONResponse:
    """Get neighbors of a node (multi-hop supported via ?depth=)."""
    graph = _memory_graph(request)
    if graph is None:
        return _not_wired(request)
    depth = int(request.query_params.get("depth", 1))
    relation = request.query_params.get("relation") or None
    min_strength = float(request.query_params.get("min_strength", 0.0))
    neighbors = await graph.get_neighbors(
        node_id,
        relation=relation,
        depth=depth,
        min_strength=min_strength,
    )
    return JSONResponse({
        "neighbors": [
            {
                "edge": {
                    "id": e.id,
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "relation": e.relation,
                    "strength": round(e.strength, 3),
                },
                "node": {
                    "id": n.id,
                    "type": n.type,
                    "content": n.content[:200],
                },
            }
            for e, n in neighbors
        ],
    })


@router.get("/graph/path")
async def graph_path(request: Request) -> JSONResponse:
    """Find shortest path between two nodes.
    Query params: source_id, target_id, max_depth (default 5).
    """
    graph = _memory_graph(request)
    if graph is None:
        return _not_wired(request)
    source_id = request.query_params.get("source_id", "")
    target_id = request.query_params.get("target_id", "")
    max_depth = int(request.query_params.get("max_depth", 5))
    if not source_id or not target_id:
        return JSONResponse(
            {"error": "source_id and target_id required"},
            status_code=400,
        )
    path = await graph.find_path(source_id, target_id, max_depth=max_depth)
    if path is None:
        return JSONResponse({"path": None})
    return JSONResponse({
        "path": [
            {
                "id": e.id,
                "source_id": e.source_id,
                "target_id": e.target_id,
                "relation": e.relation,
                "strength": round(e.strength, 3),
            }
            for e in path
        ],
    })


# ── real-time websocket push ──────────────────────────────────────

@router.websocket("/ws")
async def cognition_ws(websocket: WebSocket) -> None:
    """Push attention focus, goals, and fatigue in real-time.

    Clients receive a JSON frame every ``PUSH_INTERVAL_S`` seconds
    (default 2) containing the full cognitive state snapshot.  No
    authentication beyond the standard pairing-token query param —
    the cognition dashboard is treated as a first-class UI surface.
    """
    # Fix audit 2026-06-11: add pairing-token auth. HTTP middleware
    # doesn't cover WebSocket routes; without this any localhost page
    # can stream live cognitive state (goals, focus, suggestions).
    try:
        # 2026-06-21 fix: the previous code imported ``_load_expected_token``
        # from ``middleware.pairing_auth`` — a name that NEVER existed there
        # (that module only exposes the HTTP middleware class). So this
        # import raised ImportError on EVERY connect, the except below fired,
        # and the cognition dashboard WS was closed 4401 for everyone —
        # including valid tokens. Use the canonical pairing helpers instead.
        from xmclaw.daemon.pairing import read_token, validate_token
        _expected = read_token()
        if not _expected:
            await websocket.close(code=4403, reason="no pairing token configured")
            return
        _provided = websocket.query_params.get("token", "")
        if not validate_token(_expected, _provided):
            await websocket.close(code=4401, reason="unauthorized")
            return
    except Exception:
        await websocket.close(code=4401, reason="auth error")
        return

    await websocket.accept()
    PUSH_INTERVAL_S = 2.0
    try:
        while True:
            cs = _cognitive_state(websocket)
            if cs is not None:
                # Match the shape of GET /state so the frontend can
                # use one parser for both REST and WS.
                # Match the shape of GET /state so the frontend can
                # use one parser for both REST and WS.
                payload = {
                    "goals": [
                        {
                            "id": g.id,
                            "description": g.description,
                            "priority": g.priority,
                            "source": g.source,
                            "created_at": getattr(g, "created_at", None),
                            "status": g.status,
                        }
                        for g in getattr(cs, "current_goals", [])
                    ],
                    "attention_focus": [
                        {
                            "percept_id": a.percept_id,
                            "content": getattr(a, "content", ""),
                            "salience_score": round(getattr(a, "salience_score", 0.0), 3),
                            "timestamp": getattr(a, "timestamp", None),
                        }
                        for a in getattr(cs, "attention_focus", [])
                    ],
                    "fatigue": getattr(cs, "fatigue", {}),
                    "salience_threshold": getattr(cs, "salience_threshold", 0.3),
                    "attention_capacity": getattr(cs, "attention_capacity", 7),
                }
                await websocket.send_json(payload)
            else:
                await websocket.send_json({"error": "cognition not wired"})
            await asyncio.sleep(PUSH_INTERVAL_S)
    except WebSocketDisconnect:
        pass


# ── R5: Suggestion inbox ─────────────────────────────────────────


def _suggestion_inbox(request: Request) -> Any:
    return getattr(request.app.state, "suggestion_inbox", None)


@router.get("/suggestions")
async def list_suggestions(
    request: Request, status: str = "pending", limit: int = 50,
) -> JSONResponse:
    """List pending (default) or by-status suggestions.

    Query params:
        status: ``pending`` (default) | ``approved`` | ``rejected``
                | ``expired`` | ``applied`` | ``all``
        limit: max rows. Default 50.
    """
    inbox = _suggestion_inbox(request)
    if inbox is None:
        return _not_wired(request)
    limit = max(1, min(500, int(limit)))
    if status == "all":
        rows = inbox.list_recent(limit=limit)
    elif status == "pending":
        rows = inbox.list_pending(limit=limit)
    else:
        if status not in (
            "approved", "rejected", "expired", "applied",
        ):
            return JSONResponse(
                {"error": f"unknown status: {status!r}"},
                status_code=400,
            )
        rows = inbox.list_recent(limit=limit, status=status)
    return JSONResponse({
        "suggestions": [
            {
                "id": s.id,
                "ts": s.ts,
                "kind": s.kind,
                "source": s.source,
                "summary": s.summary,
                "payload": s.payload,
                "risk": s.risk,
                "confidence": s.confidence,
                "verdict": s.verdict,
                "status": s.status,
                "decided_at": s.decided_at,
                "decided_by": s.decided_by,
                "applied_at": s.applied_at,
                "applied_outcome": s.applied_outcome,
            }
            for s in rows
        ],
        "count": len(rows),
        "pending_total": inbox.count_pending(),
    })


@router.post("/suggestions/{sg_id}/approve")
async def approve_suggestion(
    request: Request, sg_id: str,
) -> JSONResponse:
    """User approves a pending suggestion. Status flips to
    ``approved``; actual application is the daemon's job (routes
    into EvolutionController / PersonaStore / etc)."""
    inbox = _suggestion_inbox(request)
    if inbox is None:
        return _not_wired(request)
    ok = inbox.decide(sg_id, status="approved")
    return JSONResponse({"ok": ok, "id": sg_id, "status": "approved"})


@router.post("/suggestions/{sg_id}/reject")
async def reject_suggestion(
    request: Request, sg_id: str,
) -> JSONResponse:
    inbox = _suggestion_inbox(request)
    if inbox is None:
        return _not_wired(request)
    ok = inbox.decide(sg_id, status="rejected")
    return JSONResponse({"ok": ok, "id": sg_id, "status": "rejected"})


# ── Phase D: daemon + experiment observability ──────────────────────────


def _cognitive_daemon(request: Request) -> Any | None:
    return getattr(_state(request), "cognitive_daemon", None)


def _experiment_loop(request: Request) -> Any | None:
    return getattr(_state(request), "experiment_loop", None)


@router.get("/daemon")
async def get_daemon_status(request: Request) -> JSONResponse:
    """Return the live CognitiveDaemon tick summary + running state."""
    daemon = _cognitive_daemon(request)
    if daemon is None:
        return _not_wired(request)
    payload: dict[str, Any] = {
        "ok": True,
        "running": daemon.is_running,
        "tick_count": daemon.tick_count,
        "config": {
            "enabled": daemon.config.enabled,
            "autonomy_level": daemon.config.autonomy_level,
            "heartbeat_hz": daemon.config.heartbeat_hz,
            "action_threshold": daemon.config.action_threshold,
            "top_k_focus": daemon.config.top_k_focus,
            "goal_gen_every_n_ticks": daemon.config.goal_gen_every_n_ticks,
            "self_experiment_every_n_ticks": (
                daemon.config.self_experiment_every_n_ticks
            ),
            "skill_propose_every_n_ticks": (
                daemon.config.skill_propose_every_n_ticks
            ),
            "slow_subsystem_threshold_ms": (
                daemon.config.slow_subsystem_threshold_ms
            ),
        },
    }
    # Attach the most recent tick summary if available.
    last = getattr(daemon, "_last_tick_summary", None)
    if last is not None:
        payload["last_tick"] = {
            "tick": last.get("tick"),
            "n_percepts": last.get("n_percepts"),
            "n_plans_executed": last.get("n_plans_executed"),
            "ran_experiment": last.get("ran_experiment"),
            "n_reflections": last.get("n_reflections"),
            "n_skill_proposals": last.get("n_skill_proposals"),
            "latency_ms": last.get("latency_ms"),
            "errors": last.get("errors", []),
        }
    return JSONResponse(payload)


@router.get("/daemon/history")
async def get_daemon_history(
    request: Request,
    limit: int = 50,
    since: float | None = None,
    until: float | None = None,
) -> JSONResponse:
    """Query persisted tick summaries for trend analysis.

    Query params:
      * ``since`` — UNIX timestamp (inclusive)
      * ``until`` — UNIX timestamp (inclusive)
      * ``limit`` — max rows, clamped to 1..100
    """
    daemon = _cognitive_daemon(request)
    if daemon is None:
        return _not_wired(request)
    store = getattr(daemon, "_tick_store", None)
    if store is None:
        return JSONResponse(
            {"ok": False, "error": "tick_store not wired"},
            status_code=503,
        )
    ticks = await store.list_ticks(
        since=since,
        until=until,
        limit=max(1, min(limit, 100)),
    )
    return JSONResponse({"ok": True, "ticks": ticks, "count": len(ticks)})


@router.get("/daemon/health")
async def get_daemon_health(request: Request) -> JSONResponse:
    """Health check with memory + last-tick quality signal.

    Status taxonomy:
      * ``healthy``   — running and last tick was clean.
      * ``degraded``  — running but last tick had only slow-subsystem
                        warnings (no hard errors).
      * ``unhealthy`` — not running, or last tick had non-slow errors.
    """
    daemon = _cognitive_daemon(request)
    if daemon is None:
        return _not_wired(request)

    last = getattr(daemon, "_last_tick_summary", None)
    errors = last.get("errors", []) if last else []
    slow_only = bool(errors) and all(
        e.startswith("slow_subsystem:") for e in errors
    )

    if not daemon.is_running:
        status = "unhealthy"
    elif not errors:
        status = "healthy"
    elif slow_only:
        status = "degraded"
    else:
        status = "unhealthy"

    payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "running": daemon.is_running,
        "tick_count": daemon.tick_count,
    }

    if last is not None:
        payload["last_tick"] = {
            "tick": last.get("tick"),
            "latency_ms": last.get("latency_ms"),
            "errors": errors,
        }

    # Optional: process memory (RSS in MB).  psutil is an optional
    # extra (cognition-process) so we soft-fail when absent.
    try:
        import psutil as _psutil  # type: ignore[import-untyped]

        proc = _psutil.Process()
        payload["memory_mb"] = round(proc.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        pass

    return JSONResponse(payload)


@router.get("/experiments")
async def list_experiments(
    request: Request,
    limit: int = 20,
    decision: str | None = None,
) -> JSONResponse:
    """List recent A/B experiments from the SelfExperimentLoop store."""
    loop = _experiment_loop(request)
    if loop is None:
        return _not_wired(request)
    store = loop.store
    rows = await store.list_experiments(
        decision=decision,  # type: ignore[arg-type]
        limit=max(1, min(limit, 100)),
    )
    out: list[dict[str, Any]] = []
    for exp, res in rows:
        item: dict[str, Any] = {
            "id": exp.id,
            "hypothesis": exp.hypothesis,
            "metric": exp.metric,
            "suite_id": exp.suite_id,
            "started_at": exp.started_at,
        }
        if res is not None:
            item["result"] = {
                "decision": res.decision,
                "delta": res.delta,
                "delta_p_value": res.delta_p_value,
                "baseline_value": res.baseline_value,
                "treatment_value": res.treatment_value,
                "n_baseline": res.n_baseline,
                "n_treatment": res.n_treatment,
                "decision_reason": res.decision_reason,
                "finished_at": res.finished_at,
            }
        out.append(item)
    return JSONResponse({"ok": True, "experiments": out, "count": len(out)})


@router.get("/experiments/{experiment_id}")
async def get_experiment(
    request: Request, experiment_id: str,
) -> JSONResponse:
    """Get a single experiment + its result by id."""
    loop = _experiment_loop(request)
    if loop is None:
        return _not_wired(request)
    store = loop.store
    exp = await store.get_experiment(experiment_id)
    if exp is None:
        return JSONResponse(
            {"ok": False, "error": "experiment not found"},
            status_code=404,
        )
    res = await store.get_result(experiment_id)
    payload: dict[str, Any] = {
        "ok": True,
        "experiment": exp.to_dict(),
    }
    if res is not None:
        payload["result"] = res.to_dict()
    return JSONResponse(payload)


# ── Epic #26 Phase C (2026-05-19): plan history endpoints ──────────


def _plan_store(request: Request) -> Any | None:
    return getattr(request.app.state, "plan_store", None)


@router.get("/plans")
async def list_plans(
    request: Request,
    limit: int = 50,
    status: str | None = None,
) -> JSONResponse:
    """List recent autonomous plans, newest first.

    Backs the Mind page "Autonomous Tasks" panel. Each row:
    ``{plan_id, goal_id, status, started_at, finished_at, n_steps,
    n_completed, error, budget_usd, spent_usd, confidence}``.

    ``status`` filter accepts: executing / completed / failed /
    budget_exceeded / orphaned_at_restart.
    """
    store = _plan_store(request)
    if store is None:
        return JSONResponse({"plans": [], "counts": {}})
    try:
        plans = store.list_recent(limit=limit, status=status)
        counts = store.counts_by_status()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"plans": [], "counts": {}, "error": str(exc)})
    return JSONResponse({"plans": plans, "counts": counts})


@router.get("/plans/{plan_id}")
async def get_plan(request: Request, plan_id: str) -> JSONResponse:
    """Single plan row by id."""
    store = _plan_store(request)
    if store is None:
        return JSONResponse(
            {"ok": False, "error": "plan_store not wired"},
            status_code=503,
        )
    plan = store.get(plan_id)
    if plan is None:
        return JSONResponse(
            {"ok": False, "error": "plan not found"},
            status_code=404,
        )
    return JSONResponse({"ok": True, "plan": plan})
