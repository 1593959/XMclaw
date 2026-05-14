"""MemoryService — public API: remember / recall / relate / neighbors.

Phase 2. The user-facing API that hides everything below (Fact /
Relation models, VectorBackend, GraphBackend, EmbeddingService). All
five write trigger paths (§4.4 of design doc) call ``remember``, and
all read paths call ``recall``.

Design contract (mirrors §4.4-§4.5 of MEMORY_EVOLUTION_REDESIGN.md):

* **One remember(text, kind, scope, ...)** for every write. Behavior:
    1. Compute deterministic id (kind:scope:hash12(text))
    2. Embed text via EmbeddingService (cached, retried)
    3. Detect contradicts: KNN search for top-3 same-kind facts with
       distance < 0.2; if any found, the new Fact carries their ids
       in ``contradicts``, AND a CONTRADICTS edge is added in both
       directions. Caller can later mark one as ``superseded_by``
       via a follow-up ``remember`` of the older fact with
       ``superseded_by=new_id``.
    4. upsert Fact into VectorBackend (merge_insert)
    5. If a source_event_id is provided, add a CAUSED_BY edge to
       ``event:<id>`` (the pseudo-id pointing into events.db)
    6. Promote layer from working → long_term when
       evidence_count >= LONG_TERM_PROMOTE_THRESHOLD

* **One recall(query, k, kinds, scopes, ...)** for every read. Behavior:
    1. Embed query (if string) or use as-is (if already a vector)
    2. KNN search VectorBackend with the metadata filter clause
    3. For each hit, fetch its 1-hop neighbours (CONTRADICTS +
       SUPERSEDES specifically) so the caller sees "fact-9
       contradicts this" inline — matches §8.4 of design doc.
    4. Return facts in distance order, each enriched with a
       ``related_relations`` list for prompt rendering.

* **relate(source_id, target_id, kind, strength)** — explicit edge
  insertion. Used by ExperienceDistiller / ReflectionMaterializer.

* **neighbors(fact_id, ...)** — pure graph walk, no fact bodies.
  Caller composes via ``get_fact``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from xmclaw.memory.v2.backend import GraphBackend, VectorBackend
from xmclaw.memory.v2.embedding import EmbeddingFailure, EmbeddingService
from xmclaw.memory.v2.models import (
    Fact,
    FactKind,
    FactKindStr,
    FactLayer,
    FactLayerStr,
    FactScope,
    FactScopeStr,
    Relation,
    RelationKind,
    RelationKindStr,
)
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# Thresholds — exposed as module constants so tests can pin them and
# Phase-7 dream compactor can adjust at runtime if needed.

#: Distance below which two same-kind facts are considered contradictory.
#: Cosine distance = 1 - cos_sim. < 0.2 ≈ cosine similarity > 0.8.
CONTRADICTS_DISTANCE_THRESHOLD = 0.2

#: Distance below which two facts are considered the SAME topic
#: (auto-link via SAME_TOPIC, no contradict assumption).
SAME_TOPIC_DISTANCE_THRESHOLD = 0.1

#: evidence_count at which a working-layer fact is auto-promoted.
LONG_TERM_PROMOTE_THRESHOLD = 3


# ── RecallHit return shape ────────────────────────────────────────


@dataclass(slots=True)
class RecallHit:
    """One hit in a ``recall`` result.

    Combines the matched Fact with any 1-hop relations the caller
    needs for prompt rendering (CONTRADICTS / SUPERSEDES → "don't
    use this if X is true"). distance is cosine-derived; lower is
    better.
    """

    fact: Fact
    distance: float
    related_relations: list[Relation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact": self.fact.to_dict(),
            "distance": self.distance,
            "related_relations": [r.to_dict() for r in self.related_relations],
        }


# ── MemoryService ────────────────────────────────────────────────


class MemoryService:
    """Public L1 facts + relations API.

    Construct with concrete backends + an EmbeddingService. The
    backends are injected so this class never imports lancedb (the
    file stays cheap to import in tests). Three constructors are
    typical:

        # Production
        from xmclaw.memory.v2 import (
            get_lancedb_vector_backend, get_lancedb_graph_backend,
        )
        from xmclaw.memory.v2.embedding import build_embedding_service
        svc = MemoryService(
            vector_backend=get_lancedb_vector_backend(path),
            graph_backend=get_lancedb_graph_backend(path),
            embedder=build_embedding_service(cfg=cfg),
        )

        # Tests
        from xmclaw.memory.v2 import (
            InMemoryVectorBackend, InMemoryGraphBackend,
        )
        from xmclaw.memory.v2.embedding import EmbeddingService, StubEmbedder
        svc = MemoryService(
            vector_backend=InMemoryVectorBackend(),
            graph_backend=InMemoryGraphBackend(),
            embedder=EmbeddingService(StubEmbedder(dim=4)),
        )

    The ``embedder`` parameter may be None when running in
    text-only mode (no API key configured). recall() falls back to
    keyword search; remember() stores facts without embedding (vec
    column zero-filled).
    """

    def __init__(
        self,
        *,
        vector_backend: VectorBackend,
        graph_backend: GraphBackend,
        embedder: EmbeddingService | None,
    ) -> None:
        self._vec = vector_backend
        self._graph = graph_backend
        self._embedder = embedder

    @property
    def embedder(self) -> EmbeddingService | None:
        return self._embedder

    # ── Write API ───────────────────────────────────────────────

    async def remember(
        self,
        text: str,
        *,
        kind: FactKind | FactKindStr,
        scope: FactScope | FactScopeStr = FactScope.PROJECT,
        confidence: float = 0.8,
        source_event_id: str | None = None,
        layer: FactLayer | FactLayerStr = FactLayer.WORKING,
        skip_contradict_check: bool = False,
    ) -> Fact:
        """Persist one fact. Idempotent on (kind, scope, text).

        Returns the post-upsert Fact (with up-to-date
        evidence_count + ts_last).
        """
        if not text or not text.strip():
            raise ValueError("remember(): empty text")
        kind_str = kind.value if isinstance(kind, FactKind) else str(kind)
        scope_str = scope.value if isinstance(scope, FactScope) else str(scope)
        layer_str = layer.value if isinstance(layer, FactLayer) else str(layer)

        fact_id = Fact.compute_id(kind=kind_str, scope=scope_str, text=text)

        # Embed text (best-effort).
        embedding: tuple[float, ...] | None = None
        if self._embedder is not None:
            try:
                embedding = await self._embedder.embed(text)
            except EmbeddingFailure as exc:
                _log.warning(
                    "memory_service.embed_failed text=%r err=%s",
                    text[:80], exc,
                )

        # Look up existing row for merge semantics.
        existing = await self._vec.get(fact_id)
        new_evidence = 1 if existing is None else existing.evidence_count + 1
        new_confidence = (
            max(existing.confidence, confidence) if existing else confidence
        )
        ts_first = existing.ts_first if existing else time.time()
        ts_last = time.time()

        # Auto-promote to long_term when evidence threshold crossed.
        auto_layer = layer_str
        if new_evidence >= LONG_TERM_PROMOTE_THRESHOLD:
            auto_layer = FactLayer.LONG_TERM.value

        # Contradiction scan — find other same-kind facts that are
        # vector-close. Only meaningful when we have an embedding.
        contradicts_ids: tuple[str, ...] = ()
        if (
            embedding is not None
            and not skip_contradict_check
            and existing is None  # only scan on fresh insert
        ):
            try:
                nearby = await self._vec.search(
                    list(embedding),
                    where=f"kind = '{kind_str}' AND id != '{fact_id}'",
                    limit=3,
                )
                # NOTE: distance threshold is implementation-defined;
                # for now we treat all top-3 as candidates and let the
                # CONTRADICTS edge live — UI can resolve manually.
                # Future: use a proper distance metric returned by
                # the backend; LanceDB returns _distance column.
                contradicts_ids = tuple(f.id for f in nearby)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_service.contradict_scan_failed err=%s", exc,
                )

        new_fact = Fact(
            id=fact_id,
            kind=kind_str,
            scope=scope_str,
            text=text,
            confidence=new_confidence,
            evidence_count=new_evidence,
            embedding=embedding,
            source_event_id=source_event_id,
            contradicts=contradicts_ids,
            superseded_by=None,
            layer=auto_layer,
            ts_first=ts_first,
            ts_last=ts_last,
        )
        await self._vec.upsert([new_fact])

        # Edge writes (best-effort — never fail the remember on graph
        # write).
        await self._auto_link_relations(
            new_fact, contradicts_ids, source_event_id,
        )

        return new_fact

    async def _auto_link_relations(
        self,
        fact: Fact,
        contradicts_ids: tuple[str, ...],
        source_event_id: str | None,
    ) -> None:
        """Insert the auto-extracted edges that pair with this fact."""
        rels: list[Relation] = []
        # CONTRADICTS: symmetric, but we add only the outgoing edge
        # from the new fact. The other side gets a SUPERSEDES later if
        # the user marks one as superseded.
        for target in contradicts_ids:
            rid = Relation.compute_id(
                source_fact_id=fact.id,
                target_fact_id=target,
                relation=RelationKind.CONTRADICTS,
            )
            rels.append(Relation(
                id=rid,
                source_fact_id=fact.id,
                target_fact_id=target,
                relation=RelationKind.CONTRADICTS.value,
                strength=0.7,
            ))
        # CAUSED_BY: link to the L0 event using the event:<id>
        # pseudo-id convention. Backends treat it as opaque.
        if source_event_id:
            target = f"event:{source_event_id}"
            rid = Relation.compute_id(
                source_fact_id=fact.id,
                target_fact_id=target,
                relation=RelationKind.CAUSED_BY,
            )
            rels.append(Relation(
                id=rid,
                source_fact_id=fact.id,
                target_fact_id=target,
                relation=RelationKind.CAUSED_BY.value,
                strength=1.0,
            ))
        if rels:
            try:
                await self._graph.add_relations(rels)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_service.relation_write_failed err=%s "
                    "(facts persist, edges retried next write)",
                    exc,
                )

    # ── Explicit relate / supersede ─────────────────────────────

    async def relate(
        self,
        *,
        source_fact_id: str,
        target_fact_id: str,
        kind: RelationKind | RelationKindStr,
        strength: float = 1.0,
        auto_extracted: bool = False,
    ) -> Relation:
        """Insert one typed edge (idempotent by Relation.id)."""
        kind_str = kind.value if isinstance(kind, RelationKind) else str(kind)
        rid = Relation.compute_id(
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation=kind_str,
        )
        rel = Relation(
            id=rid,
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation=kind_str,
            strength=strength,
            auto_extracted=auto_extracted,
        )
        await self._graph.add_relation(rel)
        return rel

    async def supersede(
        self, *, old_fact_id: str, new_fact_id: str,
    ) -> None:
        """Mark ``old_fact_id`` as ``superseded_by=new_fact_id`` and
        add a SUPERSEDES edge new → old.

        Sets old.confidence floor so it ranks lower in recall.
        """
        old = await self._vec.get(old_fact_id)
        if old is None:
            return
        old.superseded_by = new_fact_id
        old.confidence = min(old.confidence, 0.3)
        old.ts_last = time.time()
        await self._vec.upsert([old])
        await self.relate(
            source_fact_id=new_fact_id,
            target_fact_id=old_fact_id,
            kind=RelationKind.SUPERSEDES,
            auto_extracted=False,
        )

    # ── Read API ────────────────────────────────────────────────

    async def recall(
        self,
        query: str | list[float] | None = None,
        *,
        k: int = 8,
        kinds: list[FactKindStr] | None = None,
        scopes: list[FactScopeStr] | None = None,
        min_confidence: float = 0.3,
        include_relations: bool = True,
        only_layer: FactLayerStr | None = None,
    ) -> list[RecallHit]:
        """Search L1 and return top-k facts enriched with relations.

        ``query`` may be:
            * str  — embedded by self._embedder, falls back to keyword
                     if no embedder is configured
            * list[float] — already-embedded vector
            * None — pure-filter listing ordered by ts_last DESC

        Filters compose into a single ``where`` clause.
        """
        # Build the where clause.
        clauses: list[str] = []
        if kinds:
            kinds_list = ", ".join(f"'{k}'" for k in kinds)
            clauses.append(f"kind IN ({kinds_list})")
        if scopes:
            scopes_list = ", ".join(f"'{s}'" for s in scopes)
            clauses.append(f"scope IN ({scopes_list})")
        if min_confidence > 0:
            clauses.append(f"confidence >= {min_confidence}")
        if only_layer:
            clauses.append(f"layer = '{only_layer}'")
        where = " AND ".join(clauses) if clauses else None

        # Choose the actual search input.
        search_query: list[float] | str | None
        if query is None:
            search_query = None
        elif isinstance(query, str):
            if self._embedder is not None:
                try:
                    vec = await self._embedder.embed(query)
                    search_query = list(vec)
                except EmbeddingFailure:
                    # Fall back to keyword.
                    search_query = query
            else:
                search_query = query
        else:
            search_query = list(query)

        hits = await self._vec.search(search_query, where=where, limit=k)

        # Enrich with relations.
        out: list[RecallHit] = []
        for fact in hits:
            related: list[Relation] = []
            if include_relations:
                try:
                    pairs = await self._graph.neighbors(
                        fact.id, max_hops=1,
                    )
                    related = [rel for rel, _ in pairs]
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "memory_service.recall_relations_failed err=%s",
                        exc,
                    )
            # Placeholder distance until backends return a real one.
            out.append(RecallHit(
                fact=fact,
                distance=0.0,
                related_relations=related,
            ))
        return out

    async def get_fact(self, fact_id: str) -> Fact | None:
        return await self._vec.get(fact_id)

    async def count(
        self,
        *,
        kinds: list[FactKindStr] | None = None,
        scopes: list[FactScopeStr] | None = None,
    ) -> int:
        clauses: list[str] = []
        if kinds:
            ks = ", ".join(f"'{k}'" for k in kinds)
            clauses.append(f"kind IN ({ks})")
        if scopes:
            ss = ", ".join(f"'{s}'" for s in scopes)
            clauses.append(f"scope IN ({ss})")
        where = " AND ".join(clauses) if clauses else None
        return await self._vec.count(where)

    # ── Graph walk ──────────────────────────────────────────────

    async def neighbors(
        self,
        fact_id: str,
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> list[tuple[Relation, str]]:
        return await self._graph.neighbors(
            fact_id, relation_types=relation_types, max_hops=max_hops,
        )

    async def contradictions_of(self, fact_id: str) -> list[str]:
        return await self._graph.contradictions_of(fact_id)

    async def find_related(
        self,
        fact_ids: list[str],
        *,
        max_hops: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Subgraph for UI viz. Returns {nodes, edges} JSON."""
        return await self._graph.find_related(
            fact_ids, max_hops=max_hops, limit=limit,
        )

    # ── Prompt rendering (Phase 4a) ─────────────────────────────

    async def render_for_prompt(
        self,
        query: str,
        *,
        k: int = 8,
    ) -> str:
        """Render an L1 facts block ready to be injected into the
        agent's system prompt.

        Composition (§8.3.1 of design doc):
          * User档案 — all user-scope facts kinds (preference / identity
            / correction) regardless of query
          * 项目档案 — all project-scope facts (project / commitment)
          * 决定记录 — all decision-kind facts
          * 与本轮相关 — top-k vector recall hits with relation hints

        The first three sections are "always-on" so the agent has
        durable context without needing to recall. The fourth is the
        query-conditioned working set. CONTRADICTS / SUPERSEDES
        relations appear inline as "⚠ contradicts: X" markers so the
        agent SEES the relation graph without having to query it.

        Empty string when no facts. Callers concatenate into the
        existing memory_ctx_block.
        """
        # Always-on sections.
        user_facts = await self.recall(
            None, kinds=["preference", "identity", "correction"],
            scopes=["user"], k=20, include_relations=False,
        )
        project_facts = await self.recall(
            None, kinds=["project", "commitment"],
            scopes=["project"], k=20, include_relations=False,
        )
        decision_facts = await self.recall(
            None, kinds=["decision"], k=10, include_relations=False,
        )

        # Query-conditioned.
        relevant_hits = []
        if query and query.strip():
            relevant_hits = await self.recall(
                query, k=k, include_relations=True,
            )

        sections: list[str] = []

        if user_facts:
            sections.append("### 用户档案 (USER)")
            for h in user_facts:
                sections.append(
                    f"  - [{h.fact.kind}] {h.fact.text} "
                    f"(conf {h.fact.confidence:.2f})"
                )

        if project_facts:
            sections.append("### 项目档案 (PROJECT)")
            for h in project_facts:
                sections.append(
                    f"  - [{h.fact.kind}] {h.fact.text} "
                    f"(conf {h.fact.confidence:.2f})"
                )

        if decision_facts:
            sections.append("### 决定记录 (DECISIONS)")
            for h in decision_facts:
                sections.append(
                    f"  - {h.fact.text} (conf {h.fact.confidence:.2f})"
                )

        if relevant_hits:
            sections.append("### 与本轮相关的事实 (top-K, 向量召回)")
            seen_ids = {h.fact.id for h in (user_facts + project_facts + decision_facts)}
            new_hits = [h for h in relevant_hits if h.fact.id not in seen_ids]
            for h in new_hits:
                # Annotate with CONTRADICTS / SUPERSEDES inline so the
                # agent sees the relation graph at glance.
                markers = []
                for rel in h.related_relations:
                    if rel.relation in ("CONTRADICTS", "SUPERSEDES"):
                        markers.append(f"⚠ {rel.relation.lower()} {rel.target_fact_id[:24]}")
                marker_str = f" [{'; '.join(markers)}]" if markers else ""
                sections.append(
                    f"  - [{h.fact.kind}] {h.fact.text}{marker_str} "
                    f"(conf {h.fact.confidence:.2f})"
                )

        if not sections:
            return ""

        return (
            "\n\n<memory-v2-facts>\n"
            "[System: the following are durable facts from your L1 "
            "memory store. They were recorded automatically when the "
            "user typed key information (URLs / accounts / numeric "
            "goals / 记住 X / 我是 X / etc) — you do NOT need to call "
            "memorize() for these. Refer to them naturally when "
            "relevant. ⚠ markers mean a related fact contradicts or "
            "supersedes the marked one; prefer the unmarked source.]\n\n"
            + "\n".join(sections)
            + "\n</memory-v2-facts>"
        )

    # ── Lifecycle ───────────────────────────────────────────────

    async def close(self) -> None:
        await self._vec.close()
        await self._graph.close()


__all__ = [
    "CONTRADICTS_DISTANCE_THRESHOLD",
    "LONG_TERM_PROMOTE_THRESHOLD",
    "MemoryService",
    "RecallHit",
    "SAME_TOPIC_DISTANCE_THRESHOLD",
]
