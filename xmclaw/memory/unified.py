"""Unified memory system — ``xmclaw-architecture-redesign.md`` §3.3.3.

Single entry-point combining the three indices (vector / graph /
temporal) and the four storage layers (working / short-term /
long-term / procedural). Callers express what they want as a
multi-axis query; the facade fans out to whichever provider answers
each axis, deduplicates on the unified ID, and returns
``MemoryEntry`` rows.

Iron rule (per §3.3.4 data consistency):

    Every memory entry has a globally-unique ID. Vector index, graph
    index, and temporal index all reference the SAME id when they
    point at the same logical entry. Atomic writes across the three
    indices keep them in sync; if any index fails to write, the whole
    write rolls back.

This module enforces the READ side (dedupe by id; assemble unified
view). The WRITE-side atomicity is the next ticket — for now the
facade tolerates partial writes (returns whatever each index has).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Layer = Literal["working", "short_term", "long_term", "procedural"]


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Closed range of unix timestamps. Either bound may be None
    (unbounded). Used by the temporal axis of ``query()``."""
    since: float | None = None
    until: float | None = None

    def __post_init__(self) -> None:
        if self.since is not None and self.until is not None:
            if self.since > self.until:
                raise ValueError(
                    f"TimeRange.since ({self.since}) > until ({self.until})"
                )


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """Unified memory result row. Fields populated according to which
    indices contributed. ``id`` is the unified id (per §3.3.4)."""
    id: str
    layer: Layer
    text: str
    score: float = 0.0       # similarity / relevance score, 0..1
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Provenance: which axis(es) of the query this entry hit. Useful
    # when the UI wants to show "matched semantic + temporal" badges.
    matched_axes: tuple[str, ...] = field(default_factory=tuple)


class UnifiedMemorySystem:
    """Facade over the three indices.

    Constructor parameters are duck-typed — pass any objects exposing
    the documented method shape. ``None`` means "this axis is not
    available; queries that depend on it return empty without error".

    Args:
        memory_manager: provider with
            ``async query(layer, *, text, embedding, k, filters)``.
            Powers the **vector / semantic** axis. Existing
            ``xmclaw.providers.memory.MemoryManager`` fits.
        memory_graph: provider with
            ``async get_neighbors(node_id, ...)`` /
            ``async query_by_type(type, ...)`` /
            ``async query_by_time_range(since, until, ..., limit)``.
            Powers the **graph / relational** axis AND
            **temporal** axis (graph nodes carry created_at).
        embedder: optional embedder for semantic queries
            (``async embed(texts) -> list[list[float]]``). When not
            supplied, semantic queries fall through to keyword-only.

    Iron rule #2 alignment (Sprint 3 staging gate): this class is a
    READ surface; it never writes. Writes go through the existing
    providers + their own integrity contracts.
    """

    def __init__(
        self,
        memory_manager: Any | None = None,
        memory_graph: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        self._mm = memory_manager
        self._graph = memory_graph
        self._embedder = embedder

    async def query(
        self,
        *,
        semantic: str | None = None,
        relation: str | None = None,
        temporal: TimeRange | None = None,
        layer: Layer | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Multi-axis query. At least one axis must be supplied; an
        empty query returns ``[]`` to avoid accidental whole-store
        scans.

        ``semantic``: free-text query for vector similarity search.
        ``relation``: anchor entity description; matches graph nodes
            whose content / type substring-matches and walks neighbors.
        ``temporal``: time range; pulls graph nodes whose created_at
            falls in [since, until].
        ``layer``: restrict semantic search to one storage layer.
            ``None`` searches across all layers (long_term default).
        ``limit``: max entries returned, deduplicated by id.

        Returns: list of ``MemoryEntry``, length ≤ limit, sorted by
        score descending then created_at descending. ``matched_axes``
        on each entry shows which axis(es) contributed.
        """
        if semantic is None and relation is None and temporal is None:
            return []

        # Per-axis fan-out. Each branch may be a no-op when the
        # supporting provider is None or the query doesn't use it.
        per_axis: dict[str, list[MemoryEntry]] = {}

        # ── Semantic axis (vector index) ──────────────────────────
        if semantic and self._mm is not None:
            per_axis["semantic"] = await self._semantic_query(
                semantic, layer or "long_term", limit,
            )

        # ── Relational axis (graph index) ─────────────────────────
        if relation and self._graph is not None:
            per_axis["relation"] = await self._relation_query(
                relation, limit,
            )

        # ── Temporal axis (graph rows by created_at) ──────────────
        if temporal is not None and self._graph is not None:
            per_axis["temporal"] = await self._temporal_query(
                temporal, limit,
            )

        # Merge: dedupe by id, accumulate matched_axes per entry,
        # sum scores across axes (axis-equal weighting), sort.
        merged: dict[str, MemoryEntry] = {}
        for axis_name, entries in per_axis.items():
            for e in entries:
                if e.id in merged:
                    prior = merged[e.id]
                    new_axes = tuple(
                        sorted(set(prior.matched_axes) | {axis_name})
                    )
                    merged[e.id] = MemoryEntry(
                        id=prior.id,
                        layer=prior.layer,
                        text=prior.text,
                        score=prior.score + e.score,
                        created_at=prior.created_at,
                        metadata=prior.metadata,
                        matched_axes=new_axes,
                    )
                else:
                    merged[e.id] = MemoryEntry(
                        id=e.id, layer=e.layer, text=e.text,
                        score=e.score, created_at=e.created_at,
                        metadata=e.metadata,
                        matched_axes=(axis_name,),
                    )
        ordered = sorted(
            merged.values(),
            key=lambda e: (e.score, e.created_at),
            reverse=True,
        )
        return ordered[:limit]

    # ── per-axis helpers ──────────────────────────────────────────

    async def _semantic_query(
        self, text: str, layer: Layer, k: int,
    ) -> list[MemoryEntry]:
        """Vector index query via MemoryManager. Falls back to
        keyword-only when no embedder is wired."""
        embedding: list[float] | None = None
        if self._embedder is not None:
            try:
                vectors = await self._embedder.embed([text])
                if vectors:
                    embedding = vectors[0]
            except Exception:  # noqa: BLE001 — best effort
                embedding = None
        try:
            items = await self._mm.query(
                layer, text=text, embedding=embedding, k=k,
            )
        except Exception:  # noqa: BLE001
            return []
        out: list[MemoryEntry] = []
        for item in items:
            out.append(MemoryEntry(
                id=getattr(item, "id", "") or "",
                layer=layer,
                text=getattr(item, "text", "") or "",
                score=float(getattr(item, "score", 0.0) or 0.0),
                created_at=float(getattr(item, "ts", 0.0) or 0.0),
                metadata=dict(getattr(item, "metadata", {}) or {}),
            ))
        return out

    async def _relation_query(
        self, anchor: str, k: int,
    ) -> list[MemoryEntry]:
        """Graph index query: find nodes whose content matches
        ``anchor`` (substring), walk 1-hop neighbors, return up to k.
        Score = 1.0 for direct match, 0.7 for neighbor."""
        out: list[MemoryEntry] = []
        try:
            # Direct matches: scan recent nodes; substring-match content.
            by_type_buckets = []
            for node_type in ("event", "entity", "state", "intent"):
                by_type_buckets += await self._graph.query_by_type(
                    node_type, limit=k,
                )
            anchor_lc = anchor.lower()
            direct = [
                n for n in by_type_buckets
                if anchor_lc in (n.content or "").lower()
            ]
            seen: set[str] = set()
            for n in direct:
                if n.id in seen:
                    continue
                seen.add(n.id)
                out.append(MemoryEntry(
                    id=n.id,
                    layer="long_term",
                    text=n.content,
                    score=1.0,
                    created_at=float(n.created_at or 0.0),
                    metadata={"node_type": n.type},
                ))
                # 1-hop neighbors at lower score
                neigh = await self._graph.get_neighbors(n.id)
                for m in neigh:
                    if m.id in seen or len(out) >= k:
                        continue
                    seen.add(m.id)
                    out.append(MemoryEntry(
                        id=m.id,
                        layer="long_term",
                        text=m.content,
                        score=0.7,
                        created_at=float(m.created_at or 0.0),
                        metadata={"node_type": m.type},
                    ))
                if len(out) >= k:
                    break
        except Exception:  # noqa: BLE001
            return out
        return out[:k]

    async def _temporal_query(
        self, tr: TimeRange, k: int,
    ) -> list[MemoryEntry]:
        """Time-range query via MemoryGraph.query_by_time_range.
        Score is a soft recency signal: 1.0 for the newest in-range
        entry, decaying to 0 for the oldest."""
        try:
            nodes = await self._graph.query_by_time_range(
                since=tr.since, until=tr.until, limit=k,
            )
        except Exception:  # noqa: BLE001
            return []
        if not nodes:
            return []
        max_ts = max(float(n.created_at or 0.0) for n in nodes)
        min_ts = min(float(n.created_at or 0.0) for n in nodes)
        span = max(1.0, max_ts - min_ts)
        out: list[MemoryEntry] = []
        for n in nodes:
            ts = float(n.created_at or 0.0)
            score = (ts - min_ts) / span if span > 0 else 1.0
            out.append(MemoryEntry(
                id=n.id,
                layer="long_term",
                text=n.content,
                score=float(score),
                created_at=ts,
                metadata={"node_type": n.type},
            ))
        return out
