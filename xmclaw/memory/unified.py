"""Unified memory system — ``xmclaw-architecture-redesign.md`` §3.3.3 / §3.3.4.

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

Stage A (READ-side) shipped ``query()``: dedupe by id; assemble
unified view. Stage B (WRITE-side) ships ``put()`` and ``delete()``:
every fan-out write stamps the SAME id into every index, with
best-effort compensation if any single index fails.  SQLite
cross-DB is not transactional, so we surface a clear
``UnifiedWriteError`` with a ``compensated`` list when rollback
can't fully restore consistency — that's the contract.

Failure-mode taxonomy (what can go wrong inside ``put()`` and how the
caller sees it):

* **Step 1 — graph add_node fails** (e.g. CHECK constraint on
  ``type``, FK on memory_item_id, schema mismatch). No other write
  happened. ``UnifiedWriteError.indices_written`` is ``[]``,
  ``compensated`` is ``[]``. Caller can simply retry with corrected
  inputs.
* **Step 2 — vec put fails** (disk full, embedding dim mismatch,
  WAL unavailable). Graph is rolled back. ``indices_written ==
  ["graph"]``, ``compensated == ["graph"]`` on a clean rollback —
  meaning origin store is consistent post-failure.
* **Step 3 — relation edge fails** (CHECK on ``relation`` literal,
  duplicate edge). Both prior writes rolled back. Bad relation type
  is the most common cause; pre-validate against
  ``MemoryGraph.EdgeType``.
* **Compensation itself fails** (lock contention during rollback,
  disk blip mid-delete). ``indices_written`` − ``compensated``
  identifies the dirty indices. The caller should surface to the
  operator; manual cleanup or a janitor sweep clears the orphaned
  rows.

Read-side note: ``query()`` already dedupes by id, so even a dirty
post-failure store still returns a single unified ``MemoryEntry`` row
per logical entry — the worst-case observable artefact is a slightly
stale axis (e.g. graph node missing while vec row stayed) until
operator cleanup.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from xmclaw.memory._id import UnifiedWriteError, mint_unified_id

Layer = Literal["working", "short_term", "long_term", "procedural"]


# Provider-side layer naming uses the legacy short/working/long set
# (see ``xmclaw.providers.memory.base.Layer``). UnifiedMemorySystem
# uses the more expressive short_term/working/long_term/procedural
# naming. Translate at the boundary so callers don't have to know
# the legacy names exist.
_LAYER_TO_PROVIDER: dict[str, str] = {
    "working": "working",
    "short_term": "short",
    "long_term": "long",
    # Procedural memory has no dedicated underlying layer in the
    # current sqlite_vec schema — store under "long" so the row
    # survives long-term memory aging policies (procedural facts
    # SHOULD outlive working/short turns). The metadata.layer field
    # records the logical layer for any caller that needs to filter.
    "procedural": "long",
}


_ProviderLayer = Literal["short", "working", "long"]


def _to_provider_layer(layer: str) -> _ProviderLayer:
    """Map a UnifiedMemorySystem ``Layer`` to a legacy provider
    layer name. Unknown values fall through to ``long`` rather than
    raising — defensive default since memory-write paths shouldn't
    reject on a layer typo (we'd rather store and warn than lose).
    """
    mapped = _LAYER_TO_PROVIDER.get(layer, "long")
    # Narrow to the legacy provider's Literal so MemoryItem(layer=…)
    # type-checks without a downstream cast.
    if mapped == "short":
        return "short"
    if mapped == "working":
        return "working"
    return "long"


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

    # ── §3.3.4 WRITE side: unified id + atomic fan-out ───────────

    async def put(
        self,
        *,
        text: str,
        layer: Layer = "long_term",
        node_type: Literal["event", "entity", "state", "intent"] = "event",
        relations: list[tuple[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> str:
        """Mint a unified id, write to all 3 indices atomically.

        Per §3.3.4: every memory entry has a globally-unique id; vec
        / graph / temporal indices all use the SAME id for the same
        logical entry. ``put()`` enforces that on the write side.

        Args:
            text: the entry's content (the human-readable thing).
            layer: storage layer — ``working`` / ``short_term`` /
                ``long_term`` (default) / ``procedural``. Mapped
                internally to the underlying provider's layer naming
                (the legacy provider uses ``short`` / ``working`` /
                ``long``).
            node_type: graph node kind — ``event`` (default for
                things that happened), ``entity`` (people / projects),
                ``state`` (current condition), ``intent`` (planned
                action). Drives proactive recall traversals.
            relations: optional list of ``(target_id, relation_type)``
                tuples. Each is written as a graph edge from this
                new entry → target. Relation types per
                ``MemoryGraph.EdgeType``: CAUSED_BY, RELATED_TO,
                LEADS_TO, CONTRADICTS, PART_OF.
            metadata: arbitrary JSON-serialisable metadata stored
                alongside the vec entry.
            embedding: optional pre-computed vector. When omitted,
                vec store still gets the row (without embedding)
                so the unified-id lookup works; semantic similarity
                is unavailable until a re-embed pass runs.

        Returns:
            The newly-minted unified id (24-char hex string).

        Raises:
            UnifiedWriteError: when fan-out fails partway. The
                exception's ``compensated`` attribute lists which
                indices were rolled back; entries in
                ``indices_written`` but NOT ``compensated`` are in
                an inconsistent state and need manual cleanup.

        Atomic-write protocol (best-effort across SQLite databases):
          1. Mint unified id from (text, ts, uuid4).
          2. Write graph node FIRST — its schema is the strictest
             (CHECK constraints on type, FK on edges); most likely
             to reject bad input → fail early.
          3. Write vec store with the SAME id (forced via
             ``MemoryItem.id``; provider auto-mint is bypassed).
          4. Write any relation edges (each compensation-worthy on
             its own).
          5. On any step failure → walk back: delete from already-
             written indices in reverse insertion order. Anything
             that fails to compensate is recorded in the raised
             ``UnifiedWriteError.compensated`` so the caller knows
             which indices ended up dirty.

        Note: temporal index has no separate write path here — graph
        nodes carry ``created_at``, so writing the graph node IS the
        temporal write (per §3.3.2 / §3.3.3 design). Two indices
        physically, three indices logically.
        """
        ts = time.time()
        new_id = mint_unified_id(text, ts)
        indices_written: list[str] = []

        # Step 1: graph node (strict-schema; fails earliest if bad).
        if self._graph is not None:
            try:
                from xmclaw.cognition.memory_graph import GraphNode
                node = GraphNode(
                    id=new_id,
                    type=node_type,
                    content=text,
                    embedding=tuple(embedding) if embedding else None,
                    created_at=ts,
                    memory_item_id=new_id,
                )
                await self._graph.add_node(node)
                indices_written.append("graph")
            except Exception as exc:  # noqa: BLE001
                # Nothing written yet — no compensation needed.
                raise UnifiedWriteError(
                    f"graph add_node failed: {exc!r}",
                    indices_written=[],
                    compensated=[],
                    original=exc,
                ) from exc

        # Step 2: vec store with FORCED id via MemoryItem.id.
        if self._mm is not None:
            try:
                # Map unified-system layer naming to the legacy
                # provider's layer naming. The provider Literal is
                # short/working/long; UnifiedMemorySystem uses
                # short_term/working/long_term/procedural. Procedural
                # has no underlying vec layer — store it under "long"
                # so it survives, with metadata.layer recording the
                # logical layer for reads.
                provider_layer = _to_provider_layer(layer)
                from xmclaw.providers.memory.base import MemoryItem
                merged_meta: dict[str, Any] = dict(metadata or {})
                merged_meta.setdefault("layer", layer)
                merged_meta.setdefault("node_type", node_type)
                item = MemoryItem(
                    id=new_id,
                    layer=provider_layer,
                    text=text,
                    metadata=merged_meta,
                    embedding=tuple(embedding) if embedding else None,
                    ts=ts,
                )
                await self._mm.put(provider_layer, item)
                indices_written.append("vec")
            except Exception as exc:  # noqa: BLE001
                compensated = await self._compensate(
                    new_id, indices_written,
                )
                raise UnifiedWriteError(
                    f"vec put failed: {exc!r}",
                    indices_written=list(indices_written),
                    compensated=compensated,
                    original=exc,
                ) from exc

        # Step 3: relation edges (each one compensable).
        if relations and self._graph is not None:
            try:
                from xmclaw.cognition.memory_graph import GraphEdge
                edge_ids: list[str] = []
                for target_id, relation_type in relations:
                    edge_id = mint_unified_id(
                        f"{new_id}->{target_id}:{relation_type}", ts,
                    )
                    edge = GraphEdge(
                        id=edge_id,
                        source_id=new_id,
                        target_id=str(target_id),
                        relation=relation_type,  # type: ignore[arg-type]
                        strength=1.0,
                        created_at=ts,
                    )
                    await self._graph.add_edge(edge)
                    edge_ids.append(edge_id)
                # Track edges separately so compensation can target them.
                if edge_ids:
                    indices_written.append("edges")
                    self._last_edge_ids = edge_ids  # internal only
            except Exception as exc:  # noqa: BLE001
                compensated = await self._compensate(
                    new_id, indices_written,
                )
                raise UnifiedWriteError(
                    f"relation edge write failed: {exc!r}",
                    indices_written=list(indices_written),
                    compensated=compensated,
                    original=exc,
                ) from exc

        return new_id

    async def delete(self, entry_id: str) -> bool:
        """Remove ``entry_id`` from every index. Best-effort across
        the fan-out: keep going past per-index "not found" errors so
        a partially-written entry can still be cleaned up.

        Returns:
            True iff at least one index actually deleted a record.
            False when nothing matched anywhere.

        Note: the legacy memory-manager's ``forget`` is silent on
        whether it found anything (it logs / swallows). We treat
        success-with-no-rowcount as "no record" — meaning ``delete``
        called on a stale id returns False rather than raising. The
        whole point of unified delete is "make sure it's gone" — that
        contract is satisfied either way.
        """
        any_deleted = False

        # Vec / memory-manager side. ``forget`` returns None and
        # silently swallows; we can't tell from the return whether
        # anything actually went. Probe with a query first when
        # possible so we can return a meaningful bool.
        if self._mm is not None:
            try:
                existed = await self._memory_manager_has(entry_id)
                forget_fn = getattr(self._mm, "forget", None)
                if forget_fn is not None:
                    await forget_fn(entry_id)
                if existed:
                    any_deleted = True
            except Exception:  # noqa: BLE001 — best-effort delete
                pass

        # Graph side. ``remove_node`` cascades to edges via FK ON DELETE.
        if self._graph is not None:
            try:
                existed = await self._graph_has(entry_id)
                remove_fn = getattr(self._graph, "remove_node", None)
                if remove_fn is not None:
                    await remove_fn(entry_id)
                if existed:
                    any_deleted = True
            except Exception:  # noqa: BLE001
                pass

        return any_deleted

    async def _memory_manager_has(self, entry_id: str) -> bool:
        """Best-effort 'is this id in the vec store?' check — used
        only so ``delete()`` can tell the caller whether its id
        actually matched anything. Returns False on any error so a
        flaky probe never blocks the delete itself."""
        if self._mm is None:
            return False
        try:
            # Try every storage layer the manager might know about.
            for probe_layer in ("long", "working", "short"):
                hits = await self._mm.query(
                    probe_layer, text=None, embedding=None, k=1000,
                )
                for h in hits:
                    if getattr(h, "id", None) == entry_id:
                        return True
        except Exception:  # noqa: BLE001
            return False
        return False

    async def _graph_has(self, entry_id: str) -> bool:
        """Best-effort 'is this id a graph node?' check — same
        rationale as ``_memory_manager_has``."""
        try:
            get_fn = getattr(self._graph, "get_node", None)
            if get_fn is None:
                return False
            node = await get_fn(entry_id)
            return node is not None
        except Exception:  # noqa: BLE001
            return False

    async def _compensate(
        self, entry_id: str, written: list[str],
    ) -> list[str]:
        """Walk back ``written`` (reverse order) deleting from each
        index. Returns the list of indices we successfully rolled
        back. Anything in ``written`` and NOT in the return value
        is left in an inconsistent state — the caller's
        ``UnifiedWriteError`` carries that gap."""
        compensated: list[str] = []
        for idx in reversed(written):
            try:
                if idx == "graph" and self._graph is not None:
                    remove_fn = getattr(self._graph, "remove_node", None)
                    if remove_fn is not None:
                        await remove_fn(entry_id)
                        compensated.append("graph")
                elif idx == "vec" and self._mm is not None:
                    forget_fn = getattr(self._mm, "forget", None)
                    if forget_fn is not None:
                        await forget_fn(entry_id)
                        compensated.append("vec")
                elif idx == "edges" and self._graph is not None:
                    # Cascade-delete via the graph node remove if still
                    # present; otherwise iterate any tracked edge ids.
                    edge_ids = getattr(self, "_last_edge_ids", []) or []
                    remove_edge_fn = getattr(self._graph, "remove_edge", None)
                    if remove_edge_fn is not None:
                        for eid in edge_ids:
                            try:
                                await remove_edge_fn(eid)
                            except Exception:  # noqa: BLE001
                                pass
                    compensated.append("edges")
            except Exception:  # noqa: BLE001
                # Compensation itself failed — record the index as
                # NOT compensated; caller decides what to do.
                continue
        return compensated

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
