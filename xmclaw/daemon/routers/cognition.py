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
        "leaves": [_bound_to_dict(l) for l in bound.atomic_leaves()],
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