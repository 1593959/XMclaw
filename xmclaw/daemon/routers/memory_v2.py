"""Memory v2 API — facts / relations / graph for the Memory Panel UI.

Phase 5a. Mounted at ``/api/v2/memory/v2``. Talks directly to the
``app.state.memory_v2_service`` constructed in app_lifespan when
``cognition.memory_v2.enabled`` is true. Returns 503 with a clear
"v2 disabled" message when not wired.

Endpoints:

  GET  /api/v2/memory/v2/status
       — quick "is it on?" probe for the UI. Returns
         {enabled: bool, embedder_dim: int, fact_count: int}

  GET  /api/v2/memory/v2/facts
       — list facts. Query params: kind, scope, layer, q (keyword),
         limit, offset. Returns {facts: [...], total: N}.

  GET  /api/v2/memory/v2/facts/{fact_id}
       — single fact + its 1-hop relations + the related facts'
         bodies. Returns {fact: {...}, neighbors: [{relation, fact}]}.

  POST /api/v2/memory/v2/facts
       — manual fact creation. Body: {text, kind, scope, confidence}.
         Same idempotent upsert as the daemon hook path.

  DELETE /api/v2/memory/v2/facts/{fact_id}
       — manual delete. UI uses this for the trash can on the
         Memory Panel list view.

  GET  /api/v2/memory/v2/graph
       — subgraph for vis-network. Query params: focus_fact_id,
         max_hops (default 2), limit (default 50).
         Returns {nodes: [Fact, ...], edges: [Relation, ...]}.

All responses are token-gated by ``Depends(require_pairing)`` —
same as every other v2 router. Tests under
``tests/integration/test_v2_memory_v2_router.py`` exercise the
full HTTP path (per the "tests must cross the front-back
boundary" rule in CLAUDE.md).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/memory/v2", tags=["memory-v2"])

# Wave-32+ (2026-05-19): server-side ceiling on the LLM-topic routes.
# Pre-fix the topic-name route did N sequential graph.neighbors() calls
# during cluster discovery (N = total fact count). With 900+ facts that
# routinely exceeded 60s and the browser surfaced "Failed to fetch" with
# no useful info. 55s ceiling = 1 cluster of ~5 LLM calls (each capped
# at 15s) still fits, but the route ALWAYS returns a clean JSON error
# rather than hanging the socket. Client uses 70s AbortController so
# this fires first.
_LLM_TOPIC_ROUTE_TIMEOUT_S = 55.0


def _get_service(request: Request) -> Any | None:
    """Pull the live MemoryService off app.state. None when v2 is
    not enabled in config (cognition.memory_v2.enabled=false)."""
    return getattr(request.app.state, "memory_v2_service", None)


def _v2_disabled_response() -> JSONResponse:
    return JSONResponse(
        {
            "error": "memory_v2_disabled",
            "detail": (
                "Memory v2 is not enabled. Set "
                "cognition.memory_v2.enabled=true in daemon/config.json "
                "and restart the daemon. Requires pip install "
                "'xmclaw[memory-v2]'."
            ),
        },
        status_code=503,
    )


# ── Status / health ──────────────────────────────────────────────


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    if svc is None:
        return {"enabled": False, "reason": "service not constructed"}
    try:
        count = await svc.count()
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "healthy": False,
            "error": str(exc),
        }
    embedder_dim = svc.embedder.dim if svc.embedder else 0
    embedder_name = svc.embedder.name if svc.embedder else "(none)"
    # Epic #27 sweep #7 (2026-05-19): surface backend schema state +
    # embedder circuit breaker state so the UI can show a "memory
    # degraded" banner. Pre-fix these signals existed inside the
    # backend objects but had no REST surface — daemon.log was the
    # only visibility, and operators don't tail logs.
    backend_schema_error = None
    try:
        backend = getattr(svc, "_vec", None)
        backend_schema_error = getattr(backend, "schema_error", None)
    except Exception:  # noqa: BLE001
        backend_schema_error = None
    embedder_circuit_open = False
    embedder_consecutive_failures = 0
    try:
        if svc.embedder is not None and hasattr(svc.embedder, "stats"):
            es = svc.embedder.stats()
            embedder_circuit_open = bool(
                es.get("circuit_breaker_open", False),
            )
            embedder_consecutive_failures = int(
                es.get("circuit_breaker_consecutive_failures", 0) or 0,
            )
    except Exception:  # noqa: BLE001
        pass
    degraded = bool(backend_schema_error) or embedder_circuit_open
    return {
        "enabled": True,
        "healthy": not degraded,
        "fact_count": count,
        "embedder_dim": embedder_dim,
        "embedder_name": embedder_name,
        "degraded": degraded,
        "backend_schema_error": backend_schema_error,
        "embedder_circuit_open": embedder_circuit_open,
        "embedder_consecutive_failures": embedder_consecutive_failures,
    }


# ── Embedder config inspection + test ────────────────────────────


@router.get("/embedder")
async def embedder_info(request: Request) -> dict[str, Any]:
    """Surface the active embedder config to the UI.

    Shows: provider, model, dim, base_url (masked), api_key set?
    + cache stats (hits/misses/hit_rate from EmbeddingService).
    """
    svc = _get_service(request)
    if svc is None or svc.embedder is None:
        return {
            "configured": False,
            "reason": "memory_v2 disabled or no embedder wired",
        }

    # Pull the underlying OpenAIEmbeddingProvider out of the LRU
    # wrapper so we can show model + base_url.
    inner = getattr(svc.embedder, "_provider", None)
    model = getattr(inner, "_model", None)
    base_url = getattr(inner, "_base_url", None)
    api_key = getattr(inner, "_api_key", None)
    max_batch = getattr(inner, "_max_batch_size", None)
    timeout_s = getattr(inner, "_timeout_s", None)

    masked_key = ""
    if api_key:
        masked_key = (
            api_key[:4] + "…" + api_key[-4:]
            if len(api_key) > 12
            else "(set, short)"
        )

    stats = svc.embedder.stats() if hasattr(svc.embedder, "stats") else {}

    return {
        "configured": True,
        "provider": svc.embedder.name,
        "model": model,
        "dim": svc.embedder.dim,
        "base_url": base_url,
        "api_key_set": bool(api_key),
        "api_key_masked": masked_key,
        "max_batch_size": max_batch,
        "timeout_s": timeout_s,
        "cache": {
            "hits": stats.get("cache_hits", 0),
            "misses": stats.get("cache_misses", 0),
            "hit_rate": stats.get("cache_hit_rate", 0.0),
            "size": stats.get("cache_size", 0),
            "capacity": stats.get("cache_capacity", 0),
        },
        "failures": stats.get("failures", 0),
    }


@router.post("/deduplicate")
async def deduplicate(request: Request) -> Any:
    """Trigger a bulk near-duplicate consolidation pass.

    Body (all optional):
        {"dry_run": true, "kinds": ["preference"], "scopes": ["user"],
         "distance_threshold": 0.12}

    Returns the deduplicate() report: scanned / clusters_found /
    merged + per-cluster survivor/loser detail for UI display.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    dry_run = bool(body.get("dry_run", False))
    kinds = body.get("kinds") or None
    scopes = body.get("scopes") or None
    distance_threshold = body.get("distance_threshold")
    report = await svc.deduplicate(
        kinds=kinds, scopes=scopes,
        distance_threshold=distance_threshold,
        dry_run=dry_run,
    )
    return report


@router.post("/backfill_cooccurrence_edges")
async def backfill_cooccurrence_edges(request: Request) -> Any:
    """Wave-27 fix-9: one-shot repair for legacy disconnected facts.

    Before the extractor auto-linked co-extracted facts, a single user
    message "https://x.com 账号 admin 密码 P" produced 3 disconnected
    nodes in the graph. This endpoint walks the store, groups facts by
    ``source_event_id``, and adds SAME_TOPIC edges between every pair
    in the same group.

    Body (optional): ``{"dry_run": true}`` to preview without writing.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    dry_run = bool(body.get("dry_run", False))
    return await svc.backfill_cooccurrence_edges(dry_run=dry_run)


@router.post("/llm_topic_refine")
async def llm_topic_refine(request: Request) -> Any:
    """Wave-32+ Layer 2: ask an LLM to judge borderline-same-topic
    pairs and emit SAME_TOPIC edges for the yes answers. Catches
    Chinese-paraphrase synonymy ("网址" ↔ "目标网站") that the vector
    threshold misses.

    Body (optional): ``{"budget": 20}`` to cap how many pairs go to
    the LLM. Default 20.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    agent = getattr(request.app.state, "agent", None)
    llm = getattr(agent, "_llm", None) if agent else None
    if llm is None:
        return JSONResponse(
            {"error": "no llm wired"}, status_code=503,
        )
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    budget = int(body.get("budget") or 20)
    budget = max(1, min(50, budget))
    # Wave-32+ robust envelope: catch ANY exception so the browser
    # gets a structured JSON error instead of a hung socket
    # ("Failed to fetch"). The previous version let exceptions
    # propagate, and certain provider-side issues (LLM timeout,
    # auth refresh fail, network drop) presented as opaque fetch
    # failures with no diagnostic in the UI.
    try:
        return await asyncio.wait_for(
            svc.llm_topic_refine(llm, budget=budget),
            timeout=_LLM_TOPIC_ROUTE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "llm_topic_refine.route_timeout after=%ss",
            _LLM_TOPIC_ROUTE_TIMEOUT_S,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Timeout after {int(_LLM_TOPIC_ROUTE_TIMEOUT_S)}s — "
                    "scan exceeded route ceiling. Try a smaller budget, "
                    "or rerun after the daemon finishes background work."
                ),
                "scanned_pairs": 0, "edges_added": 0,
                "llm_calls": 0, "duration_s": _LLM_TOPIC_ROUTE_TIMEOUT_S,
            },
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "llm_topic_refine.route_failed err=%s", exc,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "scanned_pairs": 0, "edges_added": 0,
                "llm_calls": 0, "duration_s": 0.0,
            },
            status_code=200,  # 200 so the UI's success-branch fires
        )


@router.post("/llm_topic_name")
async def llm_topic_name(request: Request) -> Any:
    """Wave-32+ Layer 3: find SAME_TOPIC clusters of 3+ facts that
    have no topic node yet, ask an LLM to name them, write the
    topic fact + PART_OF edges.

    Body (optional): ``{"budget": 5}`` to cap how many clusters get
    processed (each = 1 LLM call). Default 5.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    agent = getattr(request.app.state, "agent", None)
    llm = getattr(agent, "_llm", None) if agent else None
    if llm is None:
        return JSONResponse(
            {"error": "no llm wired"}, status_code=503,
        )
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    budget = int(body.get("budget") or 5)
    budget = max(1, min(20, budget))
    try:
        return await asyncio.wait_for(
            svc.llm_topic_name(llm, budget=budget),
            timeout=_LLM_TOPIC_ROUTE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "llm_topic_name.route_timeout after=%ss",
            _LLM_TOPIC_ROUTE_TIMEOUT_S,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Timeout after {int(_LLM_TOPIC_ROUTE_TIMEOUT_S)}s — "
                    "cluster discovery exceeded route ceiling. The "
                    "underlying scan can be slow on stores with 500+ "
                    "facts. Try budget=1 to start, or rerun later."
                ),
                "clusters_scanned": 0, "topics_created": 0,
                "llm_calls": 0, "duration_s": _LLM_TOPIC_ROUTE_TIMEOUT_S,
            },
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "llm_topic_name.route_failed err=%s", exc,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "clusters_scanned": 0, "topics_created": 0,
                "llm_calls": 0, "duration_s": 0.0,
            },
            status_code=200,
        )


@router.post("/entity_index_rebuild")
async def entity_index_rebuild(request: Request) -> Any:
    """Wave-32+ backfill: walk every fact in the vector store and
    re-register its text into the entity index. Use after upgrading
    to a daemon that has the entity layer (existing facts predate it
    and aren't in the in-memory index by default).

    Returns ``{ok, scanned, registered, errors, saved}``. Auto-saves
    the rebuilt index to disk so a daemon restart picks it up
    immediately without re-running the backfill.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    try:
        from xmclaw.memory.v2.entity import (
            default_entity_store_path, get_entity_store,
        )
        store = get_entity_store()
        result = await store.rebuild_from_facts(svc._vec)
        saved = store.save_to(default_entity_store_path())
        return JSONResponse({"ok": True, "saved": saved, **result})
    except Exception as exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "entity_index_rebuild.failed err=%s", exc,
        )
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=200,
        )


@router.get("/graph_positions")
async def graph_positions_get(request: Request) -> Any:
    """Wave-32+ Chunk 8: server-side position storage.

    Returns ``{ok, positions}`` where positions is a dict of
    ``fact_id → {x, y}``. Pre-fix the UI relied on localStorage
    only — open a different browser / device and you'd lose your
    careful manual layout. This endpoint mirrors the same data
    server-side so layouts sync across all clients connected to
    the same daemon.

    Stored as a plain JSON file under ``~/.xmclaw/v2/graph_positions.json``.
    No DB — single-writer, small payload, last-write-wins. The
    typical user has hundreds of nodes, JSON-on-disk is plenty.
    """
    try:
        from xmclaw.utils.paths import v2_dir
        path = v2_dir() / "graph_positions.json"
        if not path.exists():
            return JSONResponse({"ok": True, "positions": {}})
        import json as _json
        text = path.read_text(encoding="utf-8")
        data = _json.loads(text)
        positions = data.get("positions") if isinstance(data, dict) else None
        if not isinstance(positions, dict):
            positions = {}
        return JSONResponse({"ok": True, "positions": positions})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=200,
        )


@router.put("/graph_positions")
async def graph_positions_put(request: Request) -> Any:
    """Persist a snapshot of node positions. Body shape:

      {"positions": {"fact_id_1": {"x": 123, "y": -456}, ...}}

    Last-write-wins (typical: the UI snapshots on dragEnd /
    stabilization). Cap at 5000 nodes — beyond that the graph
    isn't usable anyway and we don't want unbounded disk growth.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "invalid JSON body"},
            status_code=400,
        )
    positions = body.get("positions") if isinstance(body, dict) else None
    if not isinstance(positions, dict):
        return JSONResponse(
            {"ok": False, "error": "body must include 'positions' object"},
            status_code=400,
        )
    if len(positions) > 5000:
        return JSONResponse(
            {"ok": False, "error": "too many positions (max 5000)"},
            status_code=400,
        )
    # Validate each entry — accept only well-shaped {x: number, y: number}.
    cleaned: dict[str, dict[str, float]] = {}
    for fid, pos in positions.items():
        if not isinstance(fid, str) or not isinstance(pos, dict):
            continue
        x = pos.get("x")
        y = pos.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        cleaned[fid] = {"x": round(float(x), 2), "y": round(float(y), 2)}
    try:
        from xmclaw.utils.paths import v2_dir
        path = v2_dir() / "graph_positions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        # Atomic write: tmp + rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            _json.dumps({"v": 1, "positions": cleaned}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return JSONResponse({"ok": True, "saved": len(cleaned)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=200,
        )


@router.get("/entity_index_stats")
async def entity_index_stats(request: Request) -> Any:
    """Quick read-only inspector — useful for the UI to surface
    "indexed N entities / M facts" so the user can see whether the
    layer is healthy."""
    try:
        from xmclaw.memory.v2.entity import get_entity_store
        return JSONResponse({"ok": True, **get_entity_store().stats()})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=200,
        )


@router.post("/relink_same_topic")
async def relink_same_topic(request: Request) -> Any:
    """Wave-32+ graph-connectivity backfill.

    Walks every non-superseded fact and re-runs the SAME_TOPIC
    auto-link logic with the new broader rules (drop same-kind
    restriction, raise neighbor limit, add shared-entity bridge).
    Orphan nodes in the graph view get reconnected without
    requiring the user to re-extract everything.

    Body (optional): ``{"dry_run": true}`` to preview without
    writing.

    Returns ``{ok, scanned, edges_added, dry_run}``.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    dry_run = bool(body.get("dry_run", False))
    return await svc.relink_same_topic(dry_run=dry_run)


@router.post("/clear_stale_contradicts")
async def clear_stale_contradicts(request: Request) -> Any:
    """One-shot repair for the pre-fix relation-scan bug.

    The old ``remember()`` mistakenly tagged the top-3 same-kind
    neighbours as CONTRADICTS for every new fact, producing the
    "⚠ 与 3 条事实矛盾" badge on facts that aren't actually
    contradicting anything. Hitting this endpoint zeroes
    ``contradicts`` on every non-correction fact and removes the
    matching CONTRADICTS graph edges. Correction-kind facts are
    left alone.

    Body (optional): ``{"dry_run": true}`` to preview without
    writing.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:  # noqa: BLE001
        body = {}
    dry_run = bool(body.get("dry_run", False))
    report = await svc.clear_stale_contradicts(dry_run=dry_run)
    return report


@router.post("/embedder/test")
async def embedder_test(request: Request) -> dict[str, Any]:
    """Round-trip test: embed a probe string + return dim + elapsed.

    Lets the user confirm the embedder is actually reachable without
    waiting for the next real turn.
    """
    import time as _time
    svc = _get_service(request)
    if svc is None or svc.embedder is None:
        return _v2_disabled_response()  # type: ignore[return-value]
    body = await request.json() if request.headers.get("content-length") else {}
    probe = body.get("text", "测试 embedder 是否工作 — quick probe")
    t0 = _time.perf_counter()
    try:
        vec = await svc.embedder.embed(probe)
        elapsed_ms = (_time.perf_counter() - t0) * 1000.0
        return {
            "ok": True,
            "probe_text": probe,
            "returned_dim": len(vec),
            "elapsed_ms": round(elapsed_ms, 1),
            # First 4 floats so the user can sanity-check it's a real vec.
            "sample": [round(float(v), 4) for v in list(vec)[:4]],
        }
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (_time.perf_counter() - t0) * 1000.0
        return {
            "ok": False,
            "error": str(exc),
            "elapsed_ms": round(elapsed_ms, 1),
        }


# ── List + filter facts ──────────────────────────────────────────


@router.get("/facts")
async def list_facts(
    request: Request,
    kind: str | None = Query(None),
    scope: str | None = Query(None),
    layer: str | None = Query(None),
    q: str | None = Query(None, description="Optional keyword search"),
    limit: int = Query(50, ge=1, le=500),
    include_superseded: bool = Query(
        False,
        description=(
            "Show facts that deduplicate() has marked as replaced by "
            "a survivor. Off by default — tombstone duplicates "
            "would otherwise pollute the UI list."
        ),
    ),
) -> Any:
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()

    # Convert UI filter params into recall() kinds/scopes/layer.
    kinds = [kind] if kind else None
    scopes = [scope] if scope else None
    only_layer = layer if layer in ("working", "long_term") else None

    hits = await svc.recall(
        q or None,
        k=limit,
        kinds=kinds,
        scopes=scopes,
        min_confidence=0.0,
        include_relations=False,
        only_layer=only_layer,
        # UI list view wants substring matches, not nearest-neighbour
        # similarity — q="网店" should hit "网店业务" but NOT
        # every other fact that happens to be vec-close.
        keyword_only=bool(q),
        include_superseded=include_superseded,
    )

    return {
        "facts": [
            {
                "id": h.fact.id,
                "kind": h.fact.kind,
                "scope": h.fact.scope,
                "text": h.fact.text,
                "confidence": h.fact.confidence,
                "evidence_count": h.fact.evidence_count,
                "source_event_id": h.fact.source_event_id,
                "contradicts": list(h.fact.contradicts),
                "superseded_by": h.fact.superseded_by,
                "layer": h.fact.layer,
                "ts_first": h.fact.ts_first,
                "ts_last": h.fact.ts_last,
            }
            for h in hits
        ],
        "total": len(hits),
    }


# ── Single fact + neighbors ──────────────────────────────────────


@router.get("/facts/count")
async def get_fact_count(request: Request) -> Any:
    """Phase 7.B.3 (2026-05-24): cheap row count for verify scripts.

    Returns ``{"count": int}``. Used by
    ``scripts/migrate_memory_db_to_v2.py verify`` to spot-check that
    facts actually landed after a migration. Registered BEFORE the
    catch-all ``/facts/{fact_id}`` so the literal "count" path
    doesn't get treated as a fact id.
    """
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    n = await svc.count()
    return {"count": n}


@router.get("/facts/{fact_id}")
async def get_fact_detail(
    request: Request, fact_id: str,
) -> Any:
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()

    fact = await svc.get_fact(fact_id)
    if fact is None:
        return JSONResponse(
            {"error": "not_found", "fact_id": fact_id},
            status_code=404,
        )

    pairs = await svc.neighbors(fact_id, max_hops=1)
    neighbors: list[dict[str, Any]] = []
    for rel, target_id in pairs:
        target_fact = None
        if not target_id.startswith("event:"):
            tf = await svc.get_fact(target_id)
            if tf:
                target_fact = {
                    "id": tf.id, "kind": tf.kind, "scope": tf.scope,
                    "text": tf.text,
                }
        neighbors.append({
            "relation": rel.relation,
            "strength": rel.strength,
            "target_id": target_id,
            "target_fact": target_fact,
        })

    return {
        "fact": {
            "id": fact.id,
            "kind": fact.kind,
            "scope": fact.scope,
            "text": fact.text,
            "confidence": fact.confidence,
            "evidence_count": fact.evidence_count,
            "source_event_id": fact.source_event_id,
            "contradicts": list(fact.contradicts),
            "superseded_by": fact.superseded_by,
            "layer": fact.layer,
            "ts_first": fact.ts_first,
            "ts_last": fact.ts_last,
        },
        "neighbors": neighbors,
    }


# ── Manual create ────────────────────────────────────────────────


@router.post("/facts")
async def create_fact(request: Request) -> Any:
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    body = await request.json()
    text = body.get("text")
    kind = body.get("kind")
    scope = body.get("scope", "project")
    confidence = float(body.get("confidence", 0.85))
    # Wave-27 Phase 3c: optional bucket routing label for the
    # persona renderer. Caller may omit (empty string == no
    # routing). Migration script + power users set this when
    # they want a fact to land in a specific persona MD section.
    bucket = body.get("bucket")
    if bucket is None:
        bucket = ""
    elif not isinstance(bucket, str):
        return JSONResponse(
            {"error": "invalid_bucket", "bucket": bucket}, status_code=400,
        )
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"error": "missing_text"}, status_code=400,
        )
    if kind not in (
        "preference", "decision", "identity", "commitment",
        "correction", "project", "episode", "lesson",
    ):
        return JSONResponse(
            {"error": "invalid_kind", "kind": kind}, status_code=400,
        )
    if scope not in ("user", "project", "session"):
        return JSONResponse(
            {"error": "invalid_scope", "scope": scope}, status_code=400,
        )
    # Phase 7.B.3 (2026-05-24): accept optional ``layer`` so the
    # migration script can land V1 procedure rows on the procedural
    # layer (exempt from sweep). Defaults to working — the V2 promote
    # pipeline auto-bumps to long_term on evidence_count threshold.
    layer = body.get("layer", "working")
    if layer not in ("working", "long_term", "procedural"):
        return JSONResponse(
            {"error": "invalid_layer", "layer": layer}, status_code=400,
        )
    fact = await svc.remember(
        text, kind=kind, scope=scope, layer=layer,
        confidence=confidence, bucket=bucket,
    )
    return {"created": fact.to_dict()}


# ── Manual delete ────────────────────────────────────────────────


@router.delete("/facts/{fact_id}")
async def delete_fact(
    request: Request, fact_id: str,
) -> Any:
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()
    safe = fact_id.replace("'", "''")
    n = await svc._vec.delete(f"id = '{safe}'")  # type: ignore[attr-defined]
    return {"deleted": n}


# ── Graph for UI viz ─────────────────────────────────────────────


@router.get("/graph")
async def get_graph(
    request: Request,
    focus_fact_id: str | None = Query(None),
    max_hops: int = Query(2, ge=1, le=5),
    limit: int = Query(50, ge=1, le=500),
) -> Any:
    svc = _get_service(request)
    if svc is None:
        return _v2_disabled_response()

    # Either focus around one fact (with N-hop neighbours) or return
    # a sample of all facts for an overview.
    if focus_fact_id:
        sub = await svc.find_related(
            [focus_fact_id], max_hops=max_hops, limit=limit,
        )
        node_ids = sub["nodes"]
        edges = sub["edges"]
    else:
        # No focus → sample top-N most-recent facts as nodes;
        # caller can click one to dive in.
        hits = await svc.recall(None, k=limit, include_relations=False)
        node_ids = [h.fact.id for h in hits]
        # Collect edges among the sampled nodes.
        edges = []
        for fid in node_ids:
            pairs = await svc.neighbors(fid, max_hops=1)
            for rel, _ in pairs:
                edges.append(rel)

    # Hydrate node bodies for the UI.
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fid in node_ids:
        if fid in seen:
            continue
        seen.add(fid)
        if fid.startswith("event:"):
            nodes.append({
                "id": fid,
                "kind": "event",
                "text": fid,
                "scope": "session",
            })
            continue
        f = await svc.get_fact(fid)
        if f:
            nodes.append({
                "id": f.id,
                "kind": f.kind,
                "scope": f.scope,
                "text": f.text,
                "confidence": f.confidence,
                "layer": f.layer,
                "evidence_count": f.evidence_count,
            })

    return {
        "nodes": nodes,
        "edges": [
            {
                "id": e.id,
                "source": e.source_fact_id,
                "target": e.target_fact_id,
                "relation": e.relation,
                "strength": e.strength,
                "auto_extracted": e.auto_extracted,
            }
            for e in edges
        ],
    }
