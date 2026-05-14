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

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/memory/v2", tags=["memory-v2"])


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
    return {
        "enabled": True,
        "healthy": True,
        "fact_count": count,
        "embedder_dim": embedder_dim,
        "embedder_name": embedder_name,
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
        # similarity — q="陪玩店" should hit "陪玩店业务" but NOT
        # every other fact that happens to be vec-close.
        keyword_only=bool(q),
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
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"error": "missing_text"}, status_code=400,
        )
    if kind not in (
        "preference", "decision", "identity", "commitment",
        "correction", "project", "episode",
    ):
        return JSONResponse(
            {"error": "invalid_kind", "kind": kind}, status_code=400,
        )
    if scope not in ("user", "project", "session"):
        return JSONResponse(
            {"error": "invalid_scope", "scope": scope}, status_code=400,
        )
    fact = await svc.remember(
        text, kind=kind, scope=scope, confidence=confidence,
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
