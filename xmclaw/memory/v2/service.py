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

#: Distance design (cosine = 1 - cos_sim, smaller = more similar):
#:
#:   d ≤ 0.15  → SAME FACT (different phrasing). Write-time merge
#:               collapses the new write into the existing row,
#:               bumps evidence_count. No relation edge — they're
#:               literally the same fact now.
#:
#:   0.15 < d ≤ 0.30 → SAME TOPIC. Related but distinct facts.
#:               Emit a SAME_TOPIC edge so the graph view can
#:               cluster them. No semantic claim about agreement.
#:
#:   0.15 < d ≤ 0.25 AND kind=correction → CONTRADICTS. A correction
#:               that lands close to an existing same-kind fact is
#:               saying "the old one is wrong / superseded". The
#:               relation IS honest because the caller (LLM /
#:               user) classified the signal that way; cosine
#:               alone wouldn't be enough.
#:
#:   d > 0.30  → unrelated. No edge.
#:
#: Pre-fix the thresholds were 0.20 / 0.15 / 0.10 — SAME_TOPIC sat
#: BELOW near-dup, so it could never fire (anything close enough to
#: be a same-topic match had already been merged as a near-dup).
#: Plus the old contradict scan ignored its threshold entirely and
#: stamped CONTRADICTS on the top-3 same-kind neighbours of every
#: write, which is why the UI read "与 N 条事实矛盾" on basically
#: everything. Fixed in the Wave-27 follow-up.
NEAR_DUPLICATE_DISTANCE_THRESHOLD = 0.15
SAME_TOPIC_DISTANCE_THRESHOLD = 0.30
CONTRADICTS_DISTANCE_THRESHOLD = 0.25

#: evidence_count at which a working-layer fact is auto-promoted.
LONG_TERM_PROMOTE_THRESHOLD = 3


# ── Cosine distance helper ───────────────────────────────────────


def _cosine_distance(
    a: tuple[float, ...] | list[float] | None,
    b: tuple[float, ...] | list[float] | None,
) -> float:
    """Cosine distance = 1 - cos_sim, in [0, 2]. 0 = identical."""
    if not a or not b or len(a) != len(b):
        return 2.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 2.0
    return 1.0 - (dot / (na * nb))


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

        # NEAR-DUPLICATE DETECTION (semantic consolidation).
        # If the exact id misses but a same-kind/scope row exists
        # with cosine distance < NEAR_DUPLICATE_DISTANCE_THRESHOLD,
        # treat THIS write as another evidence vote for the existing
        # row instead of creating a near-dup. This is what makes
        # "目标月流水破10万元" / "月流水破 10 万" / "业务目标:
        # 月流水破10万" collapse into ONE row instead of three.
        #
        # Skipped when:
        #   - explicit skip_contradict_check (caller wants pure insert)
        #   - no embedder configured (can't compute distance)
        #   - no embedding for new text (probe rejected)
        #   - existing exact-id hit (handled by the merge path below)
        if (
            embedding is not None
            and not skip_contradict_check
            and existing is None
        ):
            near_dup = await self._find_near_duplicate(
                embedding=embedding,
                kind=kind_str,
                scope=scope_str,
            )
            if near_dup is not None:
                # Treat as evidence vote on the existing fact.
                near_dup.evidence_count += 1
                near_dup.confidence = min(
                    0.99,
                    max(near_dup.confidence, confidence)
                    + 0.05 * min(near_dup.confidence, confidence),
                )
                near_dup.ts_last = time.time()
                # Promote layer if threshold crossed.
                if near_dup.evidence_count >= LONG_TERM_PROMOTE_THRESHOLD:
                    near_dup.layer = FactLayer.LONG_TERM.value
                await self._vec.upsert([near_dup])
                # If the caller passed a source_event_id, link the
                # new event to the SURVIVING fact via CAUSED_BY.
                if source_event_id:
                    try:
                        await self.relate(
                            source_fact_id=near_dup.id,
                            target_fact_id=f"event:{source_event_id}",
                            kind=RelationKind.CAUSED_BY,
                            auto_extracted=True,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                _log.info(
                    "memory_service.merged_near_dup id=%s "
                    "evidence=%d (new write: %r)",
                    near_dup.id[:32], near_dup.evidence_count, text[:60],
                )
                return near_dup
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

        # Relation scan — find other vector-close facts and label the
        # relationship honestly. Pre-fix bug: this used to grab the
        # top-3 same-kind neighbours and stamp CONTRADICTS on all of
        # them, ignoring the distance threshold and the actual
        # semantics. Result: every lesson read "与 3 条事实矛盾"
        # because lessons cluster tightly by topic.
        #
        # Fix:
        #   1. Cosine similarity ALONE can't tell "always X" from
        #      "never X" — they embed close. So a same-kind neighbour
        #      is much more likely a SAME_TOPIC fact than a real
        #      contradiction. Default behaviour: emit SAME_TOPIC
        #      edges below ``SAME_TOPIC_DISTANCE_THRESHOLD`` instead.
        #   2. CONTRADICTS is a strong claim. Reserve it for
        #      ``kind=correction`` writes — the LLM (or user) has
        #      already classified that signal as "stop doing X /
        #      change Y → Z", which IS the only place where the
        #      contradiction relation is honest.
        #   3. ``Fact.contradicts`` field stays populated ONLY when
        #      kind=correction; other kinds leave it empty so the UI
        #      "与 N 条事实矛盾" badge stops crying wolf.
        same_topic_ids: tuple[str, ...] = ()
        contradicts_ids: tuple[str, ...] = ()
        if (
            embedding is not None
            and not skip_contradict_check
            and existing is None  # only scan on fresh insert
        ):
            try:
                nearby = await self._vec.search(
                    list(embedding),
                    where=(
                        f"kind = '{kind_str}' AND id != '{fact_id}' "
                        f"AND superseded_by = ''"
                    ),
                    limit=3,
                )
                same: list[str] = []
                contra: list[str] = []
                for cand in nearby:
                    if cand.embedding is None:
                        continue
                    d = _cosine_distance(embedding, cand.embedding)
                    # SAME_TOPIC: tight cluster, broader than near-dup
                    # but still strongly related (cosine sim > 0.9).
                    if d <= SAME_TOPIC_DISTANCE_THRESHOLD:
                        same.append(cand.id)
                    # CONTRADICTS: only when THIS write IS a
                    # correction — the user / LLM said "stop / never /
                    # change". Use a looser threshold (cosine sim >
                    # 0.8) since corrections often phrase things
                    # differently from what they're correcting.
                    if (
                        kind_str == FactKind.CORRECTION.value
                        and d <= CONTRADICTS_DISTANCE_THRESHOLD
                    ):
                        contra.append(cand.id)
                same_topic_ids = tuple(same)
                contradicts_ids = tuple(contra)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_service.relation_scan_failed err=%s", exc,
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
            same_topic_ids=same_topic_ids,
        )

        return new_fact

    async def _find_near_duplicate(
        self,
        *,
        embedding: tuple[float, ...] | list[float],
        kind: str,
        scope: str,
    ) -> Fact | None:
        """KNN lookup top-3 same-kind/scope facts; return the one
        whose cosine distance to ``embedding`` is below the near-dup
        threshold (or None).

        Skips superseded rows so a SUPERSEDES'd fact doesn't keep
        absorbing evidence.
        """
        try:
            # Filter at the SQL layer so superseded tombstones don't
            # eat one of our 3 KNN slots — the in-Python check below
            # is now belt-and-braces.
            candidates = await self._vec.search(
                list(embedding),
                where=(
                    f"kind = '{kind}' AND scope = '{scope}' "
                    f"AND superseded_by = ''"
                ),
                limit=3,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("memory_service.near_dup_search_failed err=%s", exc)
            return None
        for cand in candidates:
            if cand.superseded_by:
                continue
            if cand.embedding is None:
                continue
            d = _cosine_distance(embedding, cand.embedding)
            if d <= NEAR_DUPLICATE_DISTANCE_THRESHOLD:
                return cand
        return None

    async def _auto_link_relations(
        self,
        fact: Fact,
        contradicts_ids: tuple[str, ...],
        source_event_id: str | None,
        same_topic_ids: tuple[str, ...] = (),
    ) -> None:
        """Insert the auto-extracted edges that pair with this fact.

        Three edge types here:

        - ``SAME_TOPIC``  — default for any near-neighbour (cos sim >
          0.9). Cheap, honest, doesn't claim contradiction.
        - ``CONTRADICTS`` — only when the caller already classified
          this write as a correction (``kind=correction``) AND the
          neighbour is vector-close. The strong claim earns the strong
          label.
        - ``CAUSED_BY``   — pseudo-edge to the originating L0 event.
        """
        rels: list[Relation] = []
        # SAME_TOPIC: low-cost, high-volume association. Helps the
        # graph view + recall expansion without overstating semantics.
        for target in same_topic_ids:
            rid = Relation.compute_id(
                source_fact_id=fact.id,
                target_fact_id=target,
                relation=RelationKind.SAME_TOPIC,
            )
            rels.append(Relation(
                id=rid,
                source_fact_id=fact.id,
                target_fact_id=target,
                relation=RelationKind.SAME_TOPIC.value,
                strength=0.6,
            ))
        # CONTRADICTS: strong claim. Only populated when the caller
        # tagged kind=correction — see remember()'s relation scan.
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
                strength=0.85,
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
        keyword_only: bool = False,
        include_superseded: bool = False,
    ) -> list[RecallHit]:
        """Search L1 and return top-k facts enriched with relations.

        ``query`` may be:
            * str  — embedded by self._embedder, falls back to keyword
                     if no embedder is configured
            * list[float] — already-embedded vector
            * None — pure-filter listing ordered by ts_last DESC

        Filters compose into a single ``where`` clause.

        ``include_superseded`` (default False): facts marked as
        replaced by deduplicate() carry ``superseded_by`` pointing
        at the survivor. By default those are filtered out so the
        UI list / agent recall doesn't surface tombstone duplicates.
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
        if not include_superseded:
            # LanceDB stores the column as a non-null string ("" when
            # absent); in-memory backend exposes it the same way in
            # its row dict for filter eval. Equality on '' covers both.
            clauses.append("superseded_by = ''")
        where = " AND ".join(clauses) if clauses else None

        # Choose the actual search input.
        search_query: list[float] | str | None
        if query is None:
            search_query = None
        elif isinstance(query, str):
            if keyword_only or self._embedder is None:
                # Force the keyword path — UI list / search box uses
                # this to get substring matches rather than vector
                # nearest-neighbour (which returns everything).
                search_query = query
            else:
                try:
                    vec = await self._embedder.embed(query)
                    search_query = list(vec)
                except EmbeddingFailure:
                    # Fall back to keyword.
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
        include_superseded: bool = False,
    ) -> int:
        clauses: list[str] = []
        if kinds:
            ks = ", ".join(f"'{k}'" for k in kinds)
            clauses.append(f"kind IN ({ks})")
        if scopes:
            ss = ", ".join(f"'{s}'" for s in scopes)
            clauses.append(f"scope IN ({ss})")
        if not include_superseded:
            clauses.append("superseded_by = ''")
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

    # ── Bulk dedup (offline consolidation) ──────────────────────

    async def deduplicate(
        self,
        *,
        kinds: list[str] | None = None,
        scopes: list[str] | None = None,
        distance_threshold: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Scan all facts (optionally filtered) and merge pairs whose
        cosine distance is below ``distance_threshold`` (defaults to
        the NEAR_DUPLICATE constant).

        Algorithm: union-find by greedy pairwise scan within each
        (kind, scope) bucket. For each cluster, the survivor is the
        fact with highest evidence_count (ties → earlier ts_first).
        Loser facts get marked ``superseded_by=survivor`` and a
        SUPERSEDES edge survivor→loser is added.

        Loser evidence_count + confidence get rolled into the
        survivor (cumulative votes for the same content).

        Args:
            kinds / scopes: restrict the scan; None scans everything.
            distance_threshold: override the default.
            dry_run: when True, report what WOULD be merged without
                touching the store. Useful for previewing impact.

        Returns:
            {"scanned": N, "clusters_found": K, "merged": M,
             "actions": [{"survivor_id", "loser_ids", "size"}, ...]}.
        """
        thresh = (
            distance_threshold
            if distance_threshold is not None
            else NEAR_DUPLICATE_DISTANCE_THRESHOLD
        )

        # Pull all facts in scope. Backend's list_all isn't on the
        # Protocol surface so we go through search(None, where=...).
        clauses: list[str] = []
        if kinds:
            kinds_list = ", ".join(f"'{k}'" for k in kinds)
            clauses.append(f"kind IN ({kinds_list})")
        if scopes:
            scopes_list = ", ".join(f"'{s}'" for s in scopes)
            clauses.append(f"scope IN ({scopes_list})")
        where = " AND ".join(clauses) if clauses else None

        all_facts = await self._vec.search(
            None, where=where, limit=10000,
        )

        # Bucket by (kind, scope) so we only compare facts that
        # could plausibly be dupes.
        buckets: dict[tuple[str, str], list[Fact]] = {}
        for f in all_facts:
            if f.embedding is None:
                continue
            if f.superseded_by:
                continue
            buckets.setdefault((f.kind, f.scope), []).append(f)

        actions: list[dict[str, Any]] = []
        merged_count = 0

        for (kind, scope), facts in buckets.items():
            # Greedy clustering: iterate facts; for each, find any
            # existing cluster with a member close to it. If found,
            # add this fact to that cluster. Otherwise start a new
            # singleton cluster.
            clusters: list[list[Fact]] = []
            for f in facts:
                placed = False
                for cluster in clusters:
                    # Compare against the cluster's first (anchor) member.
                    anchor = cluster[0]
                    d = _cosine_distance(f.embedding, anchor.embedding)
                    if d <= thresh:
                        cluster.append(f)
                        placed = True
                        break
                if not placed:
                    clusters.append([f])

            # Process each cluster of size >= 2.
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                # Pick survivor: highest evidence_count, then
                # earliest ts_first (older / more established wins).
                cluster.sort(
                    key=lambda x: (-x.evidence_count, x.ts_first),
                )
                survivor = cluster[0]
                losers = cluster[1:]

                if not dry_run:
                    # Roll evidence + confidence into survivor.
                    total_evidence = sum(c.evidence_count for c in cluster)
                    max_conf = max(c.confidence for c in cluster)
                    survivor.evidence_count = total_evidence
                    survivor.confidence = min(
                        0.99,
                        max_conf + 0.05 * max(
                            0,
                            len(cluster) - 1,
                        ) * 0.05,
                    )
                    survivor.ts_last = time.time()
                    # Promote to long_term if threshold met.
                    if survivor.evidence_count >= LONG_TERM_PROMOTE_THRESHOLD:
                        survivor.layer = FactLayer.LONG_TERM.value
                    await self._vec.upsert([survivor])

                    # Mark losers superseded + add SUPERSEDES edges.
                    for loser in losers:
                        loser.superseded_by = survivor.id
                        loser.confidence = min(loser.confidence, 0.3)
                        loser.ts_last = time.time()
                        await self._vec.upsert([loser])
                        try:
                            await self.relate(
                                source_fact_id=survivor.id,
                                target_fact_id=loser.id,
                                kind=RelationKind.SUPERSEDES,
                                auto_extracted=True,
                            )
                        except Exception:  # noqa: BLE001
                            pass

                merged_count += len(losers)
                actions.append({
                    "survivor_id": survivor.id,
                    "survivor_text": survivor.text[:120],
                    "loser_ids": [c.id for c in losers],
                    "loser_texts": [c.text[:120] for c in losers],
                    "size": len(cluster),
                })

        return {
            "scanned": len(all_facts),
            "clusters_found": sum(1 for a in actions),
            "merged": merged_count,
            "dry_run": dry_run,
            "actions": actions,
        }

    # ── Backfill: co-occurrence edges for existing facts ────────

    async def backfill_cooccurrence_edges(
        self, *, dry_run: bool = False,
    ) -> dict[str, Any]:
        """Wave-27 fix-9: add SAME_TOPIC edges between facts that
        share a ``source_event_id`` but have no relation yet.

        Background: before the extractor pair-linked co-extracted
        facts, a single user message like
        "https://pw310.wxselling.com 账号 admin 密码 admin888"
        produced 3 disconnected nodes in the graph. The user
        reasonably asked "为什么没有联系" — they were all in one
        sentence, so they ARE related; the system was just
        discarding the structural signal.

        New extractor writes link them at write time. This method
        is a one-shot repair for facts already in the store.

        Returns counts + sample for the UI / CLI. dry_run mode
        previews without writing edges.
        """
        all_facts = await self._vec.search(None, where=None, limit=10000)
        # Bucket by source_event_id. Only buckets with 2+ facts are
        # candidates for co-occurrence linking. Facts with no
        # source_event_id are skipped — there's no signal that they
        # came from the same input.
        from collections import defaultdict
        buckets: dict[str, list[Fact]] = defaultdict(list)
        for f in all_facts:
            if f.source_event_id and not f.superseded_by:
                buckets[f.source_event_id].append(f)

        candidate_pairs: list[tuple[Fact, Fact]] = []
        for eid, facts in buckets.items():
            if len(facts) < 2:
                continue
            uniq = list({f.id: f for f in facts}.values())
            for i, a in enumerate(uniq):
                for b in uniq[i + 1:]:
                    if a.id == b.id:
                        continue
                    candidate_pairs.append((a, b))

        if dry_run:
            return {
                "scanned": len(all_facts),
                "buckets": len([eid for eid, fs in buckets.items() if len(fs) >= 2]),
                "would_add_edges": len(candidate_pairs) * 2,
                "sample": [
                    {
                        "source_event_id": a.source_event_id,
                        "a_text": a.text[:60],
                        "b_text": b.text[:60],
                    }
                    for a, b in candidate_pairs[:5]
                ],
                "dry_run": True,
            }

        added = 0
        skipped_existing = 0
        for a, b in candidate_pairs:
            for src, dst in ((a.id, b.id), (b.id, a.id)):
                rid = Relation.compute_id(
                    source_fact_id=src, target_fact_id=dst,
                    relation=RelationKind.SAME_TOPIC,
                )
                # add_relation is idempotent by id; we count by
                # whether the edge existed (cheap check via
                # neighbors would be O(N) per pair). Just call it
                # and trust the backend's dedup — add_relation in
                # in-memory + lancedb both upsert.
                try:
                    await self._graph.add_relation(Relation(
                        id=rid,
                        source_fact_id=src,
                        target_fact_id=dst,
                        relation=RelationKind.SAME_TOPIC.value,
                        strength=0.80,
                        auto_extracted=True,
                    ))
                    added += 1
                except Exception:  # noqa: BLE001
                    skipped_existing += 1

        return {
            "scanned": len(all_facts),
            "buckets": len([eid for eid, fs in buckets.items() if len(fs) >= 2]),
            "added_edges": added,
            "errors": skipped_existing,
            "dry_run": False,
        }

    # ── Cleanup: legacy "everyone contradicts everyone" data ────

    async def clear_stale_contradicts(
        self, *, dry_run: bool = False,
    ) -> dict[str, Any]:
        """One-shot repair for the pre-fix relation-scan bug.

        Background: an earlier ``remember()`` implementation stamped
        CONTRADICTS on the top-3 same-kind neighbours of every new
        fact regardless of distance or semantics. That left every
        non-correction fact with a non-empty ``contradicts`` field
        and a CONTRADICTS edge in the graph — the UI's "⚠ 与 N 条
        事实矛盾" badge then cried wolf on all of them.

        New writes don't have this problem (only ``kind=correction``
        produces CONTRADICTS now). This method walks the EXISTING
        store and:

          1. Zeroes ``Fact.contradicts`` on every non-correction
             fact whose field is non-empty.
          2. Removes every outgoing CONTRADICTS graph edge sourced
             from a non-correction fact.

        Correction facts are left alone — their contradicts data
        is legitimate.

        Returns a report (counts + sample) for the UI / CLI.
        """
        all_facts = await self._vec.search(None, where=None, limit=10000)
        targets: list[Fact] = []
        for f in all_facts:
            if f.kind == FactKind.CORRECTION.value:
                continue
            if not f.contradicts:
                continue
            targets.append(f)

        if dry_run:
            return {
                "scanned": len(all_facts),
                "would_clear_facts": len(targets),
                "sample": [
                    {
                        "id": t.id, "kind": t.kind,
                        "n_contradicts": len(t.contradicts),
                        "text": t.text[:80],
                    }
                    for t in targets[:5]
                ],
                "dry_run": True,
            }

        cleared_edges = 0
        for f in targets:
            # Remove the matching graph edges. Edge ids are
            # deterministic on (source, target, relation) so we can
            # rebuild + delete without an extra scan.
            for target_id in f.contradicts:
                rid = Relation.compute_id(
                    source_fact_id=f.id,
                    target_fact_id=target_id,
                    relation=RelationKind.CONTRADICTS,
                )
                try:
                    await self._graph.remove_relation(rid)
                    cleared_edges += 1
                except Exception:  # noqa: BLE001
                    # Edge may already be missing — keep going.
                    pass
            # Clear the field + re-upsert the row.
            f.contradicts = ()
            await self._vec.upsert([f])

        return {
            "scanned": len(all_facts),
            "cleared_facts": len(targets),
            "cleared_edges": cleared_edges,
            "dry_run": False,
        }

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
