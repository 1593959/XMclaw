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

import hashlib
import re
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


# Epic #27 sweep #6 (2026-05-19): centralised cap for maintenance
# scans (dedupe / backfill / one-shot repair paths). Pre-fix every
# call site used limit=10000 with no signal when the scan truncated.
# At 3000+ facts the truncation becomes silent data drop — half a
# user's facts get skipped by dedupe etc. and they don't know.
#
# Cap kept at 5000 (down from 10000) to halve the worst-case memory
# footprint per scan; combined with the new "did we hit the cap?"
# warning, operators can see when the scan is incomplete + we have
# a hook to introduce cursor-based pagination later. The real fix
# is incremental_dedup (only scan facts newer than last_run_ts) —
# logged as a follow-up; this commit is the defensive prerequisite.
_MAINTENANCE_SCAN_LIMIT = 5000


def _maybe_warn_scan_truncated(
    operation: str, n_returned: int, limit: int,
) -> None:
    """Log a warning if a maintenance scan returned exactly ``limit``
    rows — strongly suggests there's more data we didn't see, and
    the operation worked on a partial view of the fact store."""
    if n_returned >= limit:
        _log.warning(
            "memory_service.scan_truncated op=%s returned=%d limit=%d "
            "— results worked on a partial view; introduce "
            "cursor-based pagination when fact_count exceeds limit",
            operation, n_returned, limit,
        )


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

# ── Three-factor recall ranking (Phase 8 ⑦, Generative Agents) ────
# Stanford Generative Agents (arXiv:2304.03442) ranks retrieved
# memories by `recency + importance + relevance` (all weights = 1).
# We mirror that: relevance = query cosine, recency = exponential
# decay on ts_last, importance = the fact's own confidence (which
# already rises with evidence_count). Pre-Phase-8 the prompt ranker
# used relevance ALONE, so a stale-but-on-topic fact outranked a
# fresh high-confidence one. Weights are equal by default like the
# paper; tune via the constants if one factor should dominate.
RANK_W_RELEVANCE = 1.0
RANK_W_RECENCY = 1.0
RANK_W_IMPORTANCE = 1.0
#: recency half-life — a fact last touched this many seconds ago
#: contributes 0.5 recency; older decays toward 0. 7 days balances
#: "recent conversation context" against "durable profile facts"
#: (the latter also score high on importance, so they don't vanish).
RANK_RECENCY_HALFLIFE_S = 60 * 60 * 24 * 7
#: reinforcement — when a fact is actually injected into the prompt
#: as query-relevant, bump its ts_last so frequently-useful memories
#: stay "recent" (MemoryBank's recall-strengthens-memory effect,
#: arXiv:2305.10250). Only re-write if at least this many seconds
#: elapsed since the last bump, to avoid write amplification on
#: rapid multi-turn exchanges.
REINFORCE_MIN_INTERVAL_S = 60 * 30  # 30 min


def _three_factor_score(
    fact: Any,
    *,
    query_vec: list[float] | None,
    query_norm: float,
    now: float,
) -> float:
    """Generative-Agents-style score: weighted relevance + recency +
    importance. All three sub-scores are normalised to [0, 1] so the
    weights are comparable. Pure function — no I/O."""
    # relevance — query cosine, clamped to [0, 1] (negative cosine →
    # irrelevant, not anti-relevant).
    relevance = 0.0
    if query_vec and query_norm > 0:
        emb = getattr(fact, "embedding", None)
        if emb:
            dot = sum(a * b for a, b in zip(query_vec, emb))
            emb_norm = sum(v * v for v in emb) ** 0.5
            if emb_norm > 0:
                relevance = max(0.0, dot / (query_norm * emb_norm))
    # recency — exponential decay on ts_last (0.5 at one half-life).
    ts_last = float(getattr(fact, "ts_last", now) or now)
    age = max(0.0, now - ts_last)
    recency = 0.5 ** (age / RANK_RECENCY_HALFLIFE_S)
    # importance — confidence already lives in [0, 1] and climbs with
    # evidence_count, so it's a ready-made importance proxy.
    importance = max(0.0, min(1.0, float(getattr(fact, "confidence", 0.0))))
    return (
        RANK_W_RELEVANCE * relevance
        + RANK_W_RECENCY * recency
        + RANK_W_IMPORTANCE * importance
    )


# ── Wave-32+ entity-token extractor ──────────────────────────────


# Distinctive tokens we want to consider as "shared entity" bridges
# between facts. The patterns target the kinds of things humans
# naturally associate together:
#
#   * URLs / domains — clearest shared-entity signal
#   * Quoted strings (file names, account names, codes)
#   * CJK noun phrases ≥ 2 chars — "网店", "网站", "账号", "密码"
#   * ASCII identifiers ≥ 4 chars — "admin", "pw310"
#
# Common Chinese stopwords / pronouns / verbs are excluded so we
# don't accidentally bridge facts that just both contain "我们" or
# "可以". Length floor (2 CJK / 4 ASCII) filters most noise.
_ENTITY_TOKEN_PATTERNS: list = [
    # Full URLs first — the most distinctive token kind.
    re.compile(r"https?://[\w\-.:/?=&%+#~]+"),
    # Quoted strings: ", ', `, 「」, 『』.
    re.compile(r'"([^"]{2,40})"'),
    re.compile(r"'([^']{2,40})'"),
    re.compile(r"`([^`]{2,40})`"),
    re.compile(r"「([^」]{2,40})」"),
    re.compile(r"『([^』]{2,40})』"),
    # File paths / domains / kebab-case ids.
    re.compile(r"[\w\-]+\.(py|js|ts|json|md|yaml|yml|toml|txt|csv|html|css|sql|sh|bat)"),
    re.compile(r"[\w\-]{3,}\.[\w\-]{2,}(?:\.[\w\-]{2,})*"),
    # CJK noun candidates — runs of 2+ chinese chars not split by
    # punctuation. Crude but cheap.
    re.compile(r"[一-龥]{2,8}"),
    # ASCII identifiers — admin, pw310, ROOT_USER, foo_bar.
    re.compile(r"[A-Za-z][\w\-]{3,}"),
]

# Tokens we don't want to bridge on — they're too common to be
# distinctive. Lowercase comparison for ASCII.
_ENTITY_STOPWORDS: frozenset[str] = frozenset({
    # Chinese pronouns / function words / time
    "我们", "你们", "他们", "可以", "现在", "今天", "昨天", "明天",
    "这个", "那个", "什么", "怎么", "为什么", "已经", "因为",
    "所以", "如果", "或者", "然后", "之前", "之后", "里面", "外面",
    "需要", "希望", "应该", "可能", "应当", "记得",
    # Punctuation-tight pairs we don't want
    "用户", "助手",
    # English noise
    "this", "that", "with", "from", "have", "been", "will",
    "should", "would", "could", "must", "into", "what", "where",
    "when", "why", "how", "the", "and", "for", "are", "but",
    "not", "you", "can", "all", "any", "true", "false",
    "none", "null",
})


def _extract_entity_tokens(text: str) -> set[str]:
    """Return the set of distinctive entity tokens in ``text``.

    For ASCII / URL / quoted strings: emit each whole match.

    For CJK runs of length N: emit EVERY 2-char window. A naive
    greedy match would slurp "网店账号" into a single token that
    only matches itself; bi-grams of the same run are
    {"网店", "店账", "账号"} and DO cross-link to a fact that
    mentions just "网店" or just "账号". This is the standard
    Chinese-search bi-gram indexing trick.
    """
    if not text:
        return set()
    out: set[str] = set()
    for pat in _ENTITY_TOKEN_PATTERNS:
        for m in pat.finditer(text):
            tok = (m.group(1) if m.lastindex else m.group(0)).strip()
            if not tok:
                continue
            is_cjk = any(("一" <= ch <= "龥") for ch in tok)
            if is_cjk:
                # Bi-gram window. For a 2-char run we emit the
                # whole thing; for longer runs every overlapping
                # 2-char window so cross-fact bridges fire even
                # when the CJK noun appears INSIDE a longer phrase.
                if len(tok) == 2:
                    bigrams = [tok]
                else:
                    bigrams = [tok[i:i + 2] for i in range(len(tok) - 1)]
                for bg in bigrams:
                    if bg in _ENTITY_STOPWORDS:
                        continue
                    out.add(bg)
            else:
                if len(tok) < 4:
                    continue
                low = tok.lower()
                if low in _ENTITY_STOPWORDS:
                    continue
                out.add(low)
    return out


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


# ── Phase 7 V1→V2 shim plumbing ─────────────────────────────────


class MemoryServiceWriteError(Exception):
    """Raised when ``MemoryService.remember`` / ``relate`` / ``delete``
    fan-out fails partway through and best-effort compensation could
    not fully restore consistency.

    Mirror of the legacy V1 :class:`xmclaw.memory._id.UnifiedWriteError`
    contract — callers migrating from V1 can keep their existing
    error-handling shape (``indices_written`` − ``compensated``
    identifies dirty indices that need manual / janitor cleanup).

    Attributes:
        indices_written: ordered list of backend names ("vector",
            "graph") that DID receive a write before the failure.
        compensated: subset of ``indices_written`` that were
            successfully rolled back. Anything in ``indices_written``
            but NOT in ``compensated`` is the inconsistency surface
            the operator must clean up.
        cause: the underlying exception that triggered the rollback.
    """

    __slots__ = ("indices_written", "compensated", "cause")

    def __init__(
        self,
        message: str,
        *,
        indices_written: list[str] | None = None,
        compensated: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.indices_written: list[str] = list(indices_written or [])
        self.compensated: list[str] = list(compensated or [])
        self.cause: BaseException | None = cause

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"MemoryServiceWriteError({self.args[0]!r}, "
            f"indices_written={self.indices_written}, "
            f"compensated={self.compensated})"
        )


#: V1 ``MemoryExtractor`` / ``UnifiedMemorySystem.put`` accepted a
#: ``node_type`` parameter taken from MemoryGraph.EdgeType (free-form
#: short strings like "fact" / "preference" / "decision" / ...). V2's
#: equivalent is the strict :class:`FactKind` enum. This table maps
#: the legacy strings to V2 ``FactKindStr`` values so Phase 7.A.3
#: migrations can drop in a single helper call.
#:
#: Unknown / legacy values map to ``"lesson"`` — the V2 default for
#: workflow / tool-quirk / failure-mode rows that V1 grouped under
#: generic node types. Callers that need stricter handling should
#: validate upstream.
_LEGACY_NODE_TYPE_TO_KIND: dict[str, "FactKindStr"] = {
    "fact": "lesson",
    "preference": "preference",
    "decision": "decision",
    "identity": "identity",
    "commitment": "commitment",
    "correction": "correction",
    "project": "project",
    "episode": "episode",
    "lesson": "lesson",
    "persona_manual": "persona_manual",
    # Legacy V1 buckets that don't have a strict V2 equivalent — fall
    # back to "lesson" so the fact is at least addressable.
    "observation": "lesson",
    "summary": "lesson",
    "rule": "lesson",
    "workflow": "lesson",
    "tool_quirk": "lesson",
    "failure_mode": "lesson",
    "value": "lesson",
}


def legacy_node_type_to_kind(node_type: str | None) -> "FactKindStr":
    """Translate a V1 ``node_type`` string into a V2 ``FactKindStr``.

    Phase 7.A.3 helper. Unknown / empty values default to
    ``"lesson"`` (the V2 generic bucket). The mapping is intentionally
    forgiving — V1's node_type was free-form, so we never raise.
    """
    if not node_type:
        return "lesson"
    return _LEGACY_NODE_TYPE_TO_KIND.get(str(node_type).lower(), "lesson")


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
        bus: Any | None = None,
    ) -> None:
        self._vec = vector_backend
        self._graph = graph_backend
        self._embedder = embedder
        # 2026-05-26: optional bus for curation event publishing
        # (MEMORY_FORGOT / MEMORY_CORRECTED / MEMORY_DEDUPED). When
        # None, the new APIs operate exactly as before — they just
        # don't surface on the "记忆活动" UI timeline. Late-bindable
        # via :meth:`set_bus` because the daemon wires the bus
        # before the service in some startup paths.
        self._bus = bus
        # 2026-05-29: optional LLM for semantic (paraphrase-level)
        # dedup. Cosine-clustering misses "空消息超过3轮停止分析" vs
        # "连续3次空消息后中止" (same meaning, similarity < 0.86).
        # When an LLM is wired, ``llm_dedup_scope`` asks it to cluster
        # paraphrases. None → that method raises a clear "no llm"
        # error; embedding-based ``dedup_scope`` still works.
        self._llm: Any | None = None

    def set_bus(self, bus: Any) -> None:
        """Late-binding hook for the optional event bus. Lifespan
        wiring calls this once the bus is ready."""
        self._bus = bus

    def set_llm(self, llm: Any) -> None:
        """Late-binding hook for the optional LLM used by
        ``llm_dedup_scope``. Lifespan wires this with the aux/fast
        tier model so semantic dedup doesn't burn flagship rates."""
        self._llm = llm

    @property
    def embedder(self) -> EmbeddingService | None:
        return self._embedder

    # ── Bucket inference / backfill (Wave-27 fix-12 follow-up) ──

    @staticmethod
    def _infer_bucket(kind: str, scope: str) -> str:
        """Default bucket assignment from (kind, scope).

        Pre-fix every extractor + manual write site duplicated this
        rule; several missed it and persisted ``bucket=''``, breaking
        the ``v2_renderer.render_affected_files`` routing — the agent
        learned facts into LanceDB but the persona MD files never
        rendered. Centralised here so any caller of
        :meth:`remember` automatically gets the right routing label
        without having to know about ``BUCKET_TO_FILE``.

        Mapping mirrors :data:`xmclaw.core.persona.v2_renderer.BUCKET_TO_FILE`:
          * ``kind=identity, scope=session`` → ``agent_identity`` → IDENTITY.md
          * ``kind=identity, scope=user``    → ``user_identity``  → USER.md
          * ``kind=preference, scope=user``  → ``user_preference`` → USER.md

        Any other combination returns "" (no routing) — the fact is
        still persisted but no MD file is regenerated for it.
        """
        if kind == "identity":
            if scope == "session":
                return "agent_identity"
            if scope == "user":
                return "user_identity"
        elif kind == "preference" and scope == "user":
            return "user_preference"
        return ""

    async def backfill_buckets(self) -> int:
        """One-shot migration: scan facts with empty bucket and
        write back the inferred value.

        Idempotent — facts that already have a bucket are skipped;
        facts where (kind, scope) yields no bucket are also skipped.
        Cheap enough to run on every daemon boot.

        Returns the number of facts actually updated.

        Wave-27 fix-12 follow-up (2026-05-19): pre-fix every fact
        written before the bucket inference shipped (or by callers
        that didn't supply bucket) sat at ``bucket=''`` — invisible
        to the persona renderer. User report: 5 facts in LanceDB,
        all bucket='', IDENTITY.md / USER.md stayed pristine
        template forever. This method heals that legacy data
        without forcing a fresh-start.
        """
        if self._vec is None:
            return 0
        try:
            rows = await self._vec.search(
                query=None,
                where="bucket = '' OR bucket IS NULL",
                limit=10000,
            )
        except Exception:  # noqa: BLE001 — InMemory backends may
            # not support the same where syntax; fall back to a full
            # scan client-side.
            try:
                rows = await self._vec.search(query=None, limit=10000)
            except Exception:  # noqa: BLE001
                return 0
        updated: list[Fact] = []
        for f in rows:
            if getattr(f, "bucket", "") or "":
                continue
            inferred = self._infer_bucket(f.kind, f.scope)
            if not inferred:
                continue
            from dataclasses import replace as _replace
            updated.append(_replace(f, bucket=inferred))
        if updated:
            try:
                await self._vec.upsert(updated)
            except Exception:  # noqa: BLE001
                return 0
        return len(updated)

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
        bucket: str = "",
    ) -> Fact:
        """Persist one fact. Idempotent on (kind, scope, text).

        Returns the post-upsert Fact (with up-to-date
        evidence_count + ts_last).

        ``bucket`` (Wave-27 fix-12, memory v3 phase 1.3): persona
        renderer routing label. See ``xmclaw.memory.v2.buckets.BUCKETS``
        for the registered set. The rendered persona MD files
        (IDENTITY.md / USER.md / etc.) query by ``bucket`` to build
        their auto sections.

        **2026-05-28 memory v3 phase 1.3**: ``bucket=""`` was the
        "dark fact" source — facts that landed in LanceDB but never
        in any .md, so the agent never saw them without explicit
        ``memory_search``. We now coerce empty/unknown buckets to
        ``misc`` (rendered to ``MEMORY.md ## Other facts (recent)``)
        so every fact has a render destination.
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
                # Wave-32+ graph-connectivity fix: drop the same-kind
                # restriction. The whole point of SAME_TOPIC is to
                # cluster facts about the same THING — a URL and a
                # credential about that URL embed in different kind
                # buckets but are obviously the same topic. Old
                # behaviour produced orphan nodes (user screenshot:
                # "目标网站无需验证码即可访问" floated alone while the
                # URL + credentials clustered without it).
                # Also raised limit 3 → 10 so wider clusters form;
                # the user's reasonable mental model is that 5+
                # related facts should all be interconnected, not
                # split into two-cluster fragments.
                nearby = await self._vec.search(
                    list(embedding),
                    where=(
                        f"id != '{fact_id}' AND superseded_by = ''"
                    ),
                    limit=10,
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
                    # change". Use a looser threshold AND keep the
                    # same-kind filter to avoid false positives
                    # (a URL "contradicting" a credential is nonsense).
                    if (
                        kind_str == FactKind.CORRECTION.value
                        and cand.kind == kind_str
                        and d <= CONTRADICTS_DISTANCE_THRESHOLD
                    ):
                        contra.append(cand.id)
                # Wave-32+ shared-entity bridge — even when vec
                # similarity falls short of the threshold, two facts
                # that mention the same URL / identifier / quoted
                # name are obviously the same topic. Catches the
                # exact case the user flagged: "目标网站无需验证码即可访问"
                # has no URL but shares the word "网址" / "网站" with
                # "网址: https://pw310...". See
                # ``_shared_entity_tokens`` for the extraction.
                entity_matches = self._shared_entity_links(
                    text, nearby, exclude=set(same),
                    new_fact_id=fact_id,
                )
                same_topic_ids = tuple(list(same) + entity_matches)
                contradicts_ids = tuple(contra)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_service.relation_scan_failed err=%s", exc,
                )

        # Wave-27 fix-12: preserve the existing bucket on idempotent
        # upsert if the new write doesn't supply one. Avoids losing
        # routing data when an extractor re-fires without bucket.
        #
        # Wave-27 fix-12 follow-up (2026-05-19): if caller didn't pass
        # a bucket AND existing fact has none, infer from (kind, scope)
        # so legacy callers (remember tool, UI panel, third-party code)
        # automatically benefit from the v2_renderer routing without
        # having to duplicate the inference logic at every call site.
        # Pre-fix every extractor had its own copy of this rule and
        # several didn't (leaving bucket='' on the persisted Fact,
        # which silently broke the persona-renderer routing — agent
        # would learn "AI 叫小咪" into LanceDB but IDENTITY.md
        # stayed pristine forever).
        # 2026-05-28 memory v3 phase 1.3: 3-tier bucket resolution.
        # Order: explicit caller > existing row's bucket >
        # kind+scope inference > "misc" (catch-all). The final
        # ``misc`` fallback closes the dark-fact loophole — pre-v3,
        # facts that fell through all three earlier tiers persisted
        # with ``bucket=""`` and were invisible to the persona
        # renderer (no .md → not in system prompt). Unknown bucket
        # names from the LLM also coerce to misc (logged once).
        from xmclaw.memory.v2.buckets import DEFAULT_BUCKET, is_known
        effective_bucket = (
            bucket
            or (existing.bucket if existing else "")
            or self._infer_bucket(kind_str, scope_str)
            or DEFAULT_BUCKET
        )
        if not is_known(effective_bucket):
            _log.info(
                "memory_service.remember.unknown_bucket=%r "
                "(fell back to %r) text=%r",
                effective_bucket, DEFAULT_BUCKET, text[:80],
            )
            effective_bucket = DEFAULT_BUCKET
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
            bucket=effective_bucket,
            ts_first=ts_first,
            ts_last=ts_last,
        )
        await self._vec.upsert([new_fact])

        # Wave-32+ entity layer: register every entity the fact text
        # mentions. The reverse index unlocks O(1) "facts that share
        # an entity" queries, which the SAME_TOPIC bridge now uses
        # in addition to vec similarity + token overlap. Idempotent
        # for repeat writes of the same fact id.
        try:
            from xmclaw.memory.v2.entity import get_entity_store
            get_entity_store().register_fact_text(new_fact.id, text)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "memory_service.entity_register_failed err=%s", exc,
            )

        # Edge writes (best-effort — never fail the remember on graph
        # write).
        await self._auto_link_relations(
            new_fact, contradicts_ids, source_event_id,
            same_topic_ids=same_topic_ids,
        )

        return new_fact

    async def remember_with_decision(
        self,
        text: str,
        *,
        kind: FactKind | FactKindStr,
        scope: FactScope | FactScopeStr = FactScope.PROJECT,
        confidence: float = 0.8,
        source_event_id: str | None = None,
        llm: Any | None = None,
        relate_distance: float = 0.40,
        max_neighbors: int = 8,
    ) -> dict[str, Any]:
        """Phase 8 ⑨ — Mem0-style write-time memory decision
        (arXiv:2504.19413).

        Instead of always inserting (and letting an offline sweep clean
        up later — the root cause behind the 1760-fact pile-up), we
        decide what to do with the new fact AT WRITE TIME against its
        nearest existing neighbours:

          * **ADD**    — genuinely new → insert.
          * **UPDATE** — an existing neighbour says the same thing but
            the new text is more complete → write the merged text and
            supersede the old one onto it.
          * **DELETE** — the new fact contradicts a neighbour → insert
            the new fact AND time-fail the neighbour (Zep ``invalid_at``
            — we never hard-delete; the old assertion stays for
            history).
          * **NOOP**   — already known → evidence-vote the neighbour,
            no new row.

        Cost control: the LLM is consulted ONLY when there is at least
        one *plausibly related* neighbour (cosine distance ≤
        ``relate_distance``). A fact with no close neighbour is a pure
        ADD and skips the LLM entirely. Falls back to plain
        :meth:`remember` (which still does near-dup evidence voting)
        when no LLM is wired or anything goes wrong — so this is always
        safe to call.

        Returns ``{"action", "fact", "reason"}`` where ``fact`` is the
        resulting/affected Fact (or None).
        """
        import json as _json

        kind_str = kind.value if isinstance(kind, FactKind) else str(kind)
        scope_str = scope.value if isinstance(scope, FactScope) else str(scope)
        active_llm = llm or self._llm

        async def _plain_add(reason: str) -> dict[str, Any]:
            f = await self.remember(
                text, kind=kind_str, scope=scope_str,
                confidence=confidence, source_event_id=source_event_id,
            )
            return {"action": "ADD", "fact": f, "reason": reason}

        # No LLM → fall back to the existing safe path.
        if active_llm is None or self._embedder is None:
            return await _plain_add("no_llm_or_embedder")

        # Embed + fetch plausibly-related neighbours in the same scope.
        try:
            qvec = list(await self._embedder.embed(text))
        except Exception:  # noqa: BLE001
            return await _plain_add("embed_failed")

        try:
            neighbours = await self._vec.search(
                qvec,
                where=f"scope = '{scope_str}' AND superseded_by = ''",
                limit=max_neighbors,
            )
        except Exception:  # noqa: BLE001
            return await _plain_add("neighbour_search_failed")

        # Keep only genuinely-related ones (and drop already-invalidated
        # rows — a contradicted past fact shouldn't drive the decision).
        now = time.time()
        related: list[Fact] = []
        for nb in neighbours:
            if getattr(nb, "invalid_at", None) and nb.invalid_at <= now:
                continue
            emb = getattr(nb, "embedding", None)
            if not emb:
                continue
            d = _cosine_distance(qvec, emb)
            if d <= relate_distance:
                related.append(nb)
        if not related:
            # Nothing close → pure ADD, no LLM spend.
            return await _plain_add("no_related_neighbour")

        # Ask the LLM to choose ADD / UPDATE / DELETE / NOOP.
        numbered = "\n".join(
            f"{i+1}. {nb.text}" for i, nb in enumerate(related)
        )
        system_prompt = (
            "你是记忆写入决策器(Mem0 风格)。下面有一条【新事实】和若干"
            "【已存在的相关记忆】(带编号)。判断对这条新事实该执行哪个操作,"
            "只返回纯 JSON(不要 markdown):\n"
            '{"action": "ADD|UPDATE|DELETE|NOOP", "target": 编号或null, '
            '"merged_text": "仅 UPDATE 时给出合并后的更完整表述", '
            '"reason": "简述"}\n\n'
            "规则:\n"
            "- ADD:新事实是全新信息,已有记忆里没有 → target=null。\n"
            "- UPDATE:某条已有记忆讲的是同一件事,但新事实更完整/更准 → "
            "target=该编号,merged_text=融合两者的单条规范表述。\n"
            "- DELETE:新事实与某条已有记忆**直接矛盾**(旧的过时了) → "
            "target=该矛盾编号(旧事实会被时间失效保留为历史,新事实照常写入)。\n"
            "- NOOP:已有记忆已完全覆盖该新事实,无需改动 → target=该编号。\n"
            "- 拿不准就用 ADD(保守,不丢信息)。"
        )
        user_prompt = f"【新事实】\n{text}\n\n【已存在的相关记忆】\n{numbered}"
        try:
            from xmclaw.core.ir import Message
            resp = await active_llm.complete(messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ])
            raw = (resp.content or "").strip()
            if raw.startswith("```"):
                raw = raw.removeprefix("```json").removeprefix("```")
                raw = raw.removesuffix("```").strip()
            decision = _json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — never crash a write
            _log.warning("memory_service.write_decision.llm_failed err=%s", exc)
            return await _plain_add("llm_decision_failed")

        action = str(decision.get("action") or "ADD").upper()
        target = decision.get("target")
        reason = str(decision.get("reason") or "")[:200]

        def _target_fact() -> Fact | None:
            if isinstance(target, int) and 1 <= target <= len(related):
                return related[target - 1]
            return None

        try:
            if action == "NOOP":
                tgt = _target_fact()
                if tgt is not None:
                    # Evidence-vote the existing fact (already known).
                    tgt.evidence_count += 1
                    tgt.confidence = min(
                        0.99,
                        max(tgt.confidence, confidence)
                        + 0.05 * min(tgt.confidence, confidence),
                    )
                    tgt.ts_last = now
                    if tgt.evidence_count >= LONG_TERM_PROMOTE_THRESHOLD:
                        tgt.layer = FactLayer.LONG_TERM.value
                    await self._vec.upsert([tgt])
                    return {"action": "NOOP", "fact": tgt, "reason": reason}
                return await _plain_add("noop_without_target")

            if action == "UPDATE":
                tgt = _target_fact()
                merged = str(decision.get("merged_text") or "").strip()
                if tgt is not None and merged:
                    new_fact = await self.remember(
                        merged, kind=kind_str, scope=scope_str,
                        confidence=max(confidence, tgt.confidence),
                        source_event_id=source_event_id,
                    )
                    if new_fact.id != tgt.id:
                        await self.supersede(
                            old_fact_id=tgt.id, new_fact_id=new_fact.id,
                        )
                    return {
                        "action": "UPDATE", "fact": new_fact,
                        "reason": reason,
                    }
                return await _plain_add("update_without_target_or_text")

            if action == "DELETE":
                # New fact wins; time-fail the contradicted neighbour
                # (Zep route — keep it for history).
                tgt = _target_fact()
                new_fact = await self.remember(
                    text, kind=kind_str, scope=scope_str,
                    confidence=confidence, source_event_id=source_event_id,
                )
                if tgt is not None and tgt.id != new_fact.id:
                    if tgt.invalid_at is None:
                        tgt.invalid_at = now
                    tgt.confidence = min(tgt.confidence, 0.4)
                    tgt.contradicts = tuple(
                        set(tgt.contradicts) | {new_fact.id}
                    )
                    tgt.ts_last = now
                    await self._vec.upsert([tgt])
                    try:
                        await self.relate(
                            source_fact_id=new_fact.id,
                            target_fact_id=tgt.id,
                            kind=RelationKind.CONTRADICTS,
                            auto_extracted=True,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return {
                    "action": "DELETE", "fact": new_fact, "reason": reason,
                }

            # Default / ADD.
            return await _plain_add(reason or "llm_add")
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "memory_service.write_decision.apply_failed "
                "action=%s err=%s", action, exc,
            )
            return await _plain_add("apply_failed")

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

    # ── Wave-32+ shared-entity bridge ────────────────────────────

    def _shared_entity_links(
        self,
        new_text: str,
        candidates: list[Fact],
        *,
        exclude: set[str],
        new_fact_id: str | None = None,
    ) -> list[str]:
        """Return ids of candidate facts that share at least one
        DISTINCTIVE entity reference with ``new_text``. Two layers:

          1. Wave-32+ entity-store overlap (strong signal): if the
             new fact and a candidate share a CANONICAL entity_id
             from the EntityStore reverse index, they're linked.
             This catches "https://X" vs "网址 https://X" — both
             register the same URL canonical, store returns the
             match in O(1).
          2. Fallback token overlap (weak signal): for facts that
             pre-date the entity layer or didn't get registered,
             fall back to the old `_extract_entity_tokens` token-
             intersection bridge.

        ``exclude`` is the ids already linked via the vec scan — we
        don't want to double-emit edges, the relate() upsert handles
        dedup but skipping work is cheaper.
        """
        matched_set: set[str] = set()

        # Strong: entity-store reverse index.
        if new_fact_id:
            try:
                from xmclaw.memory.v2.entity import get_entity_store
                store = get_entity_store()
                # Register this fact's entities first (idempotent),
                # then ask "which other facts share my entities?"
                store.register_fact_text(new_fact_id, new_text)
                shared = store.co_mentioned_facts(new_fact_id, exclude=exclude)
                # Keep only candidates the caller actually passed in
                # (we don't want to add edges to facts that aren't
                # in the vec-search neighbor pool — they're probably
                # superseded or far away).
                candidate_ids = {c.id for c in candidates}
                matched_set.update(shared & candidate_ids)
            except Exception:  # noqa: BLE001
                pass

        # Weak fallback: token-intersection bridge.
        new_tokens = _extract_entity_tokens(new_text)
        if new_tokens:
            for cand in candidates:
                if cand.id in exclude or cand.id in matched_set:
                    continue
                cand_tokens = _extract_entity_tokens(cand.text)
                if cand_tokens & new_tokens:
                    matched_set.add(cand.id)
        return list(matched_set)

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

    async def upsert_persona_manual(
        self, basename: str, body: str,
    ) -> Fact:
        """Wave-27 Phase 3b: store the user-edited manual section of
        a persona MD file as a v2 fact.

        Differs from ``remember()`` in TWO ways:

          1. The id is deterministic on ``basename`` (not text), so a
             second edit of IDENTITY.md REPLACES the prior row instead
             of stacking a new one. There is at most ONE manual row
             per file.

          2. Skips the near-dup / contradict scans — manual content
             is user-curated, not extracted, so vec-similarity
             collapsing across files would corrupt the routing.

        ``body`` may be empty (clears the manual section). Whitespace-
        normalised before storage so trivial edits don't create
        ghost evidence_count bumps.
        """
        if not basename:
            raise ValueError("upsert_persona_manual(): empty basename")
        clean = (body or "").rstrip()
        # Deterministic id keyed on basename — one row per file.
        # The text content goes into ``text``; ``bucket`` carries
        # the filename so the renderer can find it.
        fact_id = (
            f"persona_manual:session:"
            f"{hashlib.sha1(basename.encode('utf-8')).hexdigest()[:12]}"
        )

        # Look up existing row to preserve evidence_count + ts_first.
        existing = await self._vec.get(fact_id)
        ts_first = (
            existing.ts_first if existing is not None else time.time()
        )
        evidence = (
            existing.evidence_count + 1 if existing is not None else 1
        )

        embedding: tuple[float, ...] | None = None
        if self._embedder is not None and clean:
            try:
                # Truncate for embed call sanity (manual sections
                # can be very long).
                vec = await self._embedder.embed(clean[:6000])
                embedding = tuple(vec)
            except EmbeddingFailure as exc:
                _log.warning(
                    "memory_service.persona_manual_embed_failed "
                    "basename=%s err=%s", basename, exc,
                )

        fact = Fact(
            id=fact_id,
            kind=FactKind.PERSONA_MANUAL.value,
            scope=FactScope.SESSION.value,
            text=clean,
            confidence=1.0,           # user-curated = max confidence
            evidence_count=evidence,
            embedding=embedding,
            source_event_id=None,
            contradicts=(),
            superseded_by=None,
            layer=FactLayer.LONG_TERM.value,
            bucket=basename,          # routing key for v2_renderer
            ts_first=ts_first,
            ts_last=time.time(),
        )
        await self._vec.upsert([fact])
        return fact

    async def get_persona_manual(self, basename: str) -> Fact | None:
        """Wave-27 Phase 3b: read the manual section row for a file.

        Returns None when nothing has been written yet (fresh
        install). v2_renderer treats None as "no manual content,
        render auto sections only".
        """
        if not basename:
            return None
        fact_id = (
            f"persona_manual:session:"
            f"{hashlib.sha1(basename.encode('utf-8')).hexdigest()[:12]}"
        )
        return await self._vec.get(fact_id)

    async def supersede(
        self, *, old_fact_id: str, new_fact_id: str,
    ) -> None:
        """Mark ``old_fact_id`` as ``superseded_by=new_fact_id`` and
        add a SUPERSEDES edge new → old.

        Sets old.confidence floor so it ranks lower in recall, and
        (Phase 8 ⑩) stamps ``invalid_at`` — a superseded fact stopped
        being the current truth as of now. ``invalid_at`` keeps it
        recoverable for history (Zep route) while hiding it from
        default recall, same as ``superseded_by``.
        """
        old = await self._vec.get(old_fact_id)
        if old is None:
            return
        now = time.time()
        old.superseded_by = new_fact_id
        old.confidence = min(old.confidence, 0.3)
        old.ts_last = now
        if old.invalid_at is None:
            old.invalid_at = now
        await self._vec.upsert([old])
        await self.relate(
            source_fact_id=new_fact_id,
            target_fact_id=old_fact_id,
            kind=RelationKind.SUPERSEDES,
            auto_extracted=False,
        )

    # 2026-05-26: agent-curation APIs (chat-b3c614bc follow-up).
    # User reported the agent can ingest facts but has no surface to
    # delete / correct / dedup them. The underlying schema already
    # carried ``superseded_by`` + a ``supersede(old, new)`` API; the
    # missing pieces were:
    #   • forget-without-replacement (the "未引入新事实，旧事实就是错的"
    #     case — user simply says "我不叫张伟"). We model it as a
    #     supersede with the sentinel ``__forgotten__``; the recall
    #     filter ``superseded_by = ''`` already drops it.
    #   • correct(old_text, new_text) — caller knows the wrong text
    #     but not the fact_id. Find best match by recall, then
    #     atomically create the new fact + supersede the old one.
    #   • dedup_scope(scope, kind, dry_run) — surface-level wrapper
    #     around the existing ``deduplicate`` bulk job so the agent
    #     can trigger semantic dedup on demand for a specific
    #     (kind, scope, bucket) bucket without flushing the entire
    #     L1 store.

    _FORGOTTEN_SENTINEL = "__forgotten__"

    async def _publish_curation(
        self, kind: str, payload: dict[str, Any],
    ) -> None:
        """Best-effort publish of a memory-curation event.

        ``kind`` is "forgot" / "corrected" / "deduped" — mapped to
        the matching ``EventType.MEMORY_*`` enum value. Failures are
        swallowed; curation correctness must not depend on the bus
        being up.
        """
        if self._bus is None:
            return
        try:
            from xmclaw.core.bus.events import EventType, make_event
            type_map = {
                "forgot": EventType.MEMORY_FORGOT,
                "corrected": EventType.MEMORY_CORRECTED,
                "deduped": EventType.MEMORY_DEDUPED,
            }
            et = type_map.get(kind)
            if et is None:
                return
            await self._bus.publish(make_event(
                session_id="_memory",
                agent_id="memory_service",
                type=et,
                payload=payload,
            ))
        except Exception as _exc:  # noqa: BLE001
            try:
                from xmclaw.utils.swallowed_exceptions import (
                    record as _swallow,
                )
                _swallow("memory_service.publish_curation", _exc)
            except Exception:  # noqa: BLE001
                pass

    async def forget(
        self, *, fact_id: str, reason: str | None = None,
    ) -> bool:
        """Soft-delete a single fact — recall + persona render
        will drop it on the next read.

        Returns True when something was marked, False when the
        fact_id didn't resolve. Reason is logged but not persisted
        (we don't want forgotten facts to keep occupying bucket
        slots through a verbose reason field).
        """
        old = await self._vec.get(fact_id)
        if old is None:
            return False
        old_text = old.text or ""
        old.superseded_by = self._FORGOTTEN_SENTINEL
        old.confidence = 0.0
        old.ts_last = time.time()
        await self._vec.upsert([old])
        if reason:
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).info(
                    "memory_service.forget id=%s reason=%s",
                    fact_id, reason[:160],
                )
            except Exception:  # noqa: BLE001
                pass
        await self._publish_curation("forgot", {
            "fact_id": fact_id,
            "text": old_text[:200],
            "reason": (reason or "")[:200],
        })
        return True

    async def correct(
        self,
        *,
        old_text: str = "",
        new_text: str,
        old_fact_id: str | None = None,
        kind: FactKindStr | None = None,
        scope: FactScopeStr | None = None,
        bucket: str | None = None,
        max_match_distance: float = 0.45,
    ) -> dict[str, Any]:
        """Replace an old fact with ``new_text``, linking the two via
        SUPERSEDES.

        Old-fact location:
        * ``old_fact_id`` (preferred when known) — direct lookup, no
          embedding query. The found fact is treated as ``matched``
          unconditionally; ``max_match_distance`` is ignored.
        * ``old_text`` (fallback) — semantic search; the best hit
          within ``max_match_distance`` is the superseded fact.

        2026-05-29 cleanup: ``old_fact_id`` lets the multi-action
        ``memory(action='replace', old_fid=...)`` tool flow through
        the same supersede pipeline as the legacy ``memory_correct``
        tool. Pre-fix it took the ``forget + remember`` shortcut and
        left no SUPERSEDES edge — orphaning the relation graph.

        Returns
        -------
        dict with keys:
          ``matched`` : bool — did we find an old fact?
          ``old_fact_id`` : str — id of the superseded fact (when matched)
          ``new_fact_id`` : str — id of the survivor (always set)
          ``distance`` : float — recall score (1.0 when located by fid)

        When no fact crosses ``max_match_distance`` (text path only),
        the new fact is still written so the corrected value is
        captured; caller sees ``matched=False``.
        """
        # Stage 1: find the best old-fact match.
        best: RecallHit | None = None
        best_distance = 1.0
        matched = False
        if old_fact_id:
            old_fact = await self._vec.get(old_fact_id)
            if old_fact is not None:
                best = RecallHit(
                    fact=old_fact, distance=0.0, related_relations=[],
                )
                best_distance = 0.0
                matched = True
        else:
            hits = await self.recall(
                old_text,
                k=5,
                kinds=[kind] if kind else None,
                scopes=[scope] if scope else None,
                buckets=[bucket] if bucket else None,
                min_confidence=0.0,
                include_relations=False,
                include_superseded=False,
            )
            best = hits[0] if hits else None
            best_distance = float(getattr(best, "distance", 1.0)) if best else 1.0
            matched = best is not None and best_distance <= max_match_distance

        # Stage 2: write the new fact.
        new_fact = await self.remember(
            new_text,
            kind=kind or (best.fact.kind if best else "preference"),
            scope=scope or (best.fact.scope if best else "user"),
            bucket=bucket or (best.fact.bucket if best else ""),
            confidence=0.9,  # corrections are user-asserted → high
            source_event_id=None,
        )

        # Stage 3: supersede the matched old fact.
        if matched and best is not None and best.fact.id != new_fact.id:
            await self.supersede(
                old_fact_id=best.fact.id,
                new_fact_id=new_fact.id,
            )
        result = {
            "matched": matched,
            "old_fact_id": best.fact.id if matched and best else None,
            "new_fact_id": new_fact.id,
            "distance": round(best_distance, 3) if best else 1.0,
        }
        await self._publish_curation("corrected", {
            **result,
            "old_text": old_text[:200] if old_text else "",
            "new_text": new_text[:200],
        })
        return result

    async def dedup_scope(
        self,
        *,
        kind: FactKindStr | None = None,
        scope: FactScopeStr | None = None,
        bucket: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Surface-level wrapper around the existing semantic
        ``deduplicate`` job, restricted to one (kind, scope, bucket)
        combination so the agent can call "clean up my user-prefs
        section" without sweeping unrelated facts.

        When ``dry_run`` is True we return what would be merged but
        don't write anything — caller can decide whether to commit.
        """
        # The full ``deduplicate`` method computes embedding clusters.
        # We just need to expose a constrained entry point. Wire it
        # by listing facts in scope, calling the existing low-level
        # cluster + supersede pipeline, and counting what changed.
        hits = await self.recall(
            None,
            k=1000,
            kinds=[kind] if kind else None,
            scopes=[scope] if scope else None,
            buckets=[bucket] if bucket else None,
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        if len(hits) <= 1:
            return {
                "scanned": len(hits),
                "merged": 0,
                "dry_run": dry_run,
            }

        # Cluster by embedding cosine. Pure-python loop is fine —
        # bucket sizes are O(50) in practice, never thousands.
        from math import sqrt
        clusters: list[list[RecallHit]] = []
        SIMILARITY_THRESHOLD = 0.86  # ≈ "essentially the same fact"
        for h in hits:
            emb = h.fact.embedding
            if not emb:
                clusters.append([h])
                continue
            placed = False
            for cluster in clusters:
                ref = cluster[0].fact.embedding
                if not ref:
                    continue
                # cosine = dot / (||a||·||b||); both are stored
                # post-normalisation by remember(), but defend
                # against rare un-normalised embeddings.
                dot = sum(a * b for a, b in zip(emb, ref))
                na = sqrt(sum(a * a for a in emb))
                nb = sqrt(sum(b * b for b in ref))
                cos = dot / (na * nb) if (na and nb) else 0.0
                if cos >= SIMILARITY_THRESHOLD:
                    cluster.append(h)
                    placed = True
                    break
            if not placed:
                clusters.append([h])

        merge_groups = [c for c in clusters if len(c) > 1]
        merged_count = 0
        merge_preview: list[dict[str, Any]] = []
        for group in merge_groups:
            # Survivor: highest confidence, then most evidence, then newest.
            group_sorted = sorted(
                group,
                key=lambda h: (
                    h.fact.confidence,
                    h.fact.evidence_count,
                    h.fact.ts_last,
                ),
                reverse=True,
            )
            survivor = group_sorted[0]
            losers = group_sorted[1:]
            merge_preview.append({
                "survivor": survivor.fact.text[:120],
                "merged": [l.fact.text[:120] for l in losers],
            })
            if dry_run:
                merged_count += len(losers)
                continue
            for loser in losers:
                await self.supersede(
                    old_fact_id=loser.fact.id,
                    new_fact_id=survivor.fact.id,
                )
                merged_count += 1
        result = {
            "scanned": len(hits),
            "clusters": len(clusters),
            "merge_groups": len(merge_groups),
            "merged": merged_count,
            "dry_run": dry_run,
            "preview": merge_preview[:10],
        }
        # Only fire event for live runs — dry-runs aren't audit events.
        if not dry_run and merged_count > 0:
            await self._publish_curation("deduped", {
                "kind": kind,
                "scope": scope,
                "bucket": bucket,
                "scanned": result["scanned"],
                "merged": merged_count,
                "merge_groups": result["merge_groups"],
            })
        return result

    async def llm_dedup_scope(
        self,
        *,
        kind: FactKindStr | None = None,
        scope: FactScopeStr | None = None,
        bucket: str | None = None,
        dry_run: bool = True,
        llm: Any | None = None,
        max_facts: int = 200,
        batch_size: int = 60,
    ) -> dict[str, Any]:
        """2026-05-29 — **semantic** dedup that catches paraphrases
        cosine-clustering misses.

        Problem this solves: ``dedup_scope`` clusters by embedding
        cosine ≥ 0.86. Facts that mean the same thing but are phrased
        very differently ("空消息超过3轮停止分析" / "连续3次空消息后
        中止分析" / "若 3 轮都是空消息则停") sit below that threshold
        and survive as duplicates. Over a long session the store
        accumulates 7-8 phrasings of one rule.

        Approach: pull the facts in scope, batch them, and ask the
        LLM "which of these say essentially the same thing? group
        them, pick the clearest phrasing as canonical." Then supersede
        the non-canonical members of each group onto the canonical
        one — same supersede pipeline as ``dedup_scope`` so the
        relation graph stays consistent.

        ``llm`` arg overrides the late-bound ``self._llm``. When
        neither is set we raise a clear error rather than silently
        no-op (the caller asked for LLM dedup specifically).

        ``dry_run`` (default True) returns the proposed merge groups
        without writing — review before committing.
        """
        import json as _json

        active_llm = llm or self._llm
        if active_llm is None:
            return {
                "error": "no llm wired — call set_llm() or pass llm=",
                "scanned": 0,
                "merged": 0,
                "dry_run": dry_run,
            }

        hits = await self.recall(
            None,
            k=max_facts,
            kinds=[kind] if kind else None,
            scopes=[scope] if scope else None,
            buckets=[bucket] if bucket else None,
            min_confidence=0.0,
            include_relations=False,
            include_superseded=False,
        )
        if len(hits) <= 1:
            return {
                "scanned": len(hits),
                "merge_groups": 0,
                "merged": 0,
                "dry_run": dry_run,
            }

        # id → hit for fast lookup when applying merges.
        by_id = {h.fact.id: h for h in hits}

        # Process in batches so the prompt stays bounded. Each batch
        # is independent — we don't try to merge across batches in one
        # pass (a follow-up run catches cross-batch dups). Number the
        # facts 1..N within the batch so the LLM references compact
        # indices instead of echoing full fids.
        all_groups: list[dict[str, Any]] = []
        merged_count = 0
        for start in range(0, len(hits), batch_size):
            batch = hits[start: start + batch_size]
            numbered = "\n".join(
                f"{i+1}. {h.fact.text}"
                for i, h in enumerate(batch)
            )
            system_prompt = (
                "你是一个记忆去重助手。下面是一批已存储的事实/规则条目，"
                "每条带编号。请找出**语义上说的是同一件事**的条目分组"
                "（措辞不同没关系，只看意思）。\n\n"
                "返回纯 JSON（不要 markdown 代码块）：\n"
                '{"groups": [{"members": [编号,...], '
                '"canonical": 编号, "reason": "为什么是同一件事"}]}\n\n'
                "规则：\n"
                "1. 只把**意思相同**的归一组——意思不同的条目绝不能合并。\n"
                "2. canonical 选这组里**表述最清晰完整**的那条编号。\n"
                "3. 单独一条、没有同义条目的，不要放进任何 group。\n"
                "4. 拿不准是否同义时，**宁可不合并**（保守）。\n"
                "5. 没有任何可合并的组就返回 {\"groups\": []}。"
            )
            try:
                from xmclaw.core.ir import Message
                resp = await active_llm.complete(
                    messages=[
                        Message(role="system", content=system_prompt),
                        Message(role="user", content=numbered),
                    ],
                )
                text = (resp.content or "").strip()
                if text.startswith("```"):
                    text = text.removeprefix("```json").removeprefix("```")
                    text = text.removesuffix("```").strip()
                parsed = _json.loads(text)
            except Exception as exc:  # noqa: BLE001 — never crash dedup
                _log.warning(
                    "memory_service.llm_dedup.batch_failed err=%s", exc,
                )
                continue

            groups = parsed.get("groups")
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                members = g.get("members")
                canonical_idx = g.get("canonical")
                if (
                    not isinstance(members, list)
                    or len(members) < 2
                    or not isinstance(canonical_idx, int)
                ):
                    continue
                # Map 1-based batch indices → facts. Guard out-of-range.
                def _fact_at(idx: int):
                    if 1 <= idx <= len(batch):
                        return batch[idx - 1].fact
                    return None

                canonical_fact = _fact_at(canonical_idx)
                if canonical_fact is None:
                    continue
                loser_facts = [
                    _fact_at(m) for m in members if m != canonical_idx
                ]
                loser_facts = [f for f in loser_facts if f is not None]
                if not loser_facts:
                    continue
                all_groups.append({
                    "canonical": canonical_fact.text[:120],
                    "merged": [f.text[:120] for f in loser_facts],
                    "reason": str(g.get("reason", ""))[:200],
                })
                if not dry_run:
                    for lf in loser_facts:
                        if lf.id == canonical_fact.id:
                            continue
                        await self.supersede(
                            old_fact_id=lf.id,
                            new_fact_id=canonical_fact.id,
                        )
                        merged_count += 1
                else:
                    merged_count += len(loser_facts)

        result = {
            "scanned": len(hits),
            "merge_groups": len(all_groups),
            "merged": merged_count,
            "dry_run": dry_run,
            "preview": all_groups[:15],
            "method": "llm_semantic",
        }
        if not dry_run and merged_count > 0:
            await self._publish_curation("deduped", {
                "kind": kind,
                "scope": scope,
                "bucket": bucket,
                "scanned": result["scanned"],
                "merged": merged_count,
                "merge_groups": result["merge_groups"],
                "method": "llm_semantic",
            })
        return result

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
        include_invalidated: bool = False,
        buckets: list[str] | None = None,
        time_range: tuple[float | None, float | None] | None = None,
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
        if buckets:
            # Wave-27 fix-12: persona renderer pulls a bucket
            # subset (e.g. ``bucket="agent_identity"`` for
            # IDENTITY.md auto section). Empty-string matches
            # default-bucket facts; explicit "" passed in
            # ``buckets`` matches those.
            buckets_list = ", ".join(f"'{b}'" for b in buckets)
            clauses.append(f"bucket IN ({buckets_list})")
        if min_confidence > 0:
            clauses.append(f"confidence >= {min_confidence}")
        if only_layer:
            clauses.append(f"layer = '{only_layer}'")
        if not include_superseded:
            # LanceDB stores the column as a non-null string ("" when
            # absent); in-memory backend exposes it the same way in
            # its row dict for filter eval. Equality on '' covers both.
            clauses.append("superseded_by = ''")
        if time_range is not None:
            # Phase 7 shim (P0 #1): time-window filter on ts_last.
            # ``(start, None)`` = since-start; ``(None, end)`` = until-end;
            # ``(start, end)`` = bounded window. Both endpoints inclusive
            # to mirror the V1 ``UnifiedMemorySystem`` TimeRange contract.
            # Native path — no V1 bridge — because Fact already has
            # ts_last on every row and both backends (InMemory eval +
            # LanceDB SQL) accept >= / <= against it.
            start, end = time_range
            if start is not None:
                clauses.append(f"ts_last >= {float(start)}")
            if end is not None:
                clauses.append(f"ts_last <= {float(end)}")
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

        # Phase 8 ⑩ (2026-05-30): temporal-validity filter. A fact whose
        # ``invalid_at`` is set and already in the past has been
        # contradicted/superseded by a newer assertion (Zep route). We
        # hide it from default recall but KEEP it on disk for history —
        # never delete on contradiction. Post-filter (not SQL) so it's
        # backend-agnostic and avoids an OR clause the where-parser
        # doesn't support.
        if not include_invalidated:
            _now = time.time()
            hits = [
                f for f in hits
                if not (
                    getattr(f, "invalid_at", None)
                    and f.invalid_at <= _now
                )
            ]

        # Enrich with relations.
        # Epic #27 sweep #4 (2026-05-19): the pre-fix loop ran
        # `await self._graph.neighbors(fact.id)` N times sequentially
        # for k=8 hits — daemon.log showed `memory_recall` at 175s
        # on a 902-fact store. LanceDB serves concurrent reads fine;
        # fan-out via asyncio.gather + Semaphore(20) cuts the worst
        # case to a single round-trip's worth of latency. Same pattern
        # llm_topic.py uses for its `_neighbors_many` batch helper.
        related_map: dict[str, list[Relation]] = {}
        if include_relations and hits:
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(20)

            async def _one(fid: str) -> tuple[str, list[Relation]]:
                async with sem:
                    try:
                        pairs = await self._graph.neighbors(
                            fid, max_hops=1,
                        )
                        return fid, [rel for rel, _ in pairs]
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "memory_service.recall_relations_failed err=%s",
                            exc,
                        )
                        return fid, []

            results = await _asyncio.gather(
                *(_one(f.id) for f in hits)
            )
            related_map = dict(results)

        out: list[RecallHit] = []
        for fact in hits:
            out.append(RecallHit(
                fact=fact,
                distance=0.0,
                related_relations=related_map.get(fact.id, []),
            ))
        return out

    async def get_fact(self, fact_id: str) -> Fact | None:
        return await self._vec.get(fact_id)

    async def recall_hybrid(
        self,
        query: str,
        *,
        k: int = 8,
        kinds: list[FactKindStr] | None = None,
        scopes: list[FactScopeStr] | None = None,
        buckets: list[str] | None = None,
        min_confidence: float = 0.0,
        include_superseded: bool = False,
        vec_weight: float = 0.6,
        bm25_weight: float = 0.4,
        corpus_cap: int = 500,
        bm25_deadline_s: float = 0.5,
    ) -> list[RecallHit]:
        """2026-05-28 memory v3 phase 3.2 — hybrid recall.

        Combines the existing vector recall (cosine over embeddings)
        with a BM25 keyword pass. Returns the union ranked by fused
        score. Fixes the chronic mediocre-on-Chinese-keywords and
        mediocre-on-rare-identifier failure modes of pure cosine.

        Fusion strategy: vector RANK score (1.0 for top hit,
        decreasing linearly) plus BM25 normalised score, weighted
        ``vec_weight`` / ``bm25_weight``. Defaults 0.6 / 0.4 mirror
        OpenClaw's LanceDB-Pro plugin's published numbers.

        2026-05-29 (chat-b09a3ad4 incident): hard caps tightened to
        avoid stalling the user-turn critical path.

        * ``corpus_cap`` dropped from 5000 → 500. The hybrid path
          full-scans LanceDB and rebuilds a Python BM25 index every
          call (until LanceDB native FTS lands in Phase 5). 500
          covers the recent + relevant facts for typical XMclaw
          users; bigger stores still get vector recall + BM25 over
          the most-recent slice.
        * ``bm25_deadline_s`` (new, 500ms) — wall-clock budget for
          the BM25 leg only. If the corpus scan + tokenisation +
          BM25 build exceeds this, we abandon the BM25 path and
          return the vector hits we already have. Vector recall is
          fast (O(log N) LanceDB ANN); BM25 is the failure mode.

        When ``rank_bm25`` isn't installed, this degrades cleanly to
        the standard vector path — no error, no warning beyond the
        one-time bm25.is_available() log. Callers don't have to
        branch on availability.

        Filters compose like ``recall``: kinds / scopes / buckets /
        min_confidence / include_superseded.
        """
        if not isinstance(query, str) or not query.strip():
            return []
        # Always run the vector path first — it produces the candidate
        # pool we score BM25 against AND the RecallHit objects we
        # return. We pull 3× k to leave room for BM25 to swap in
        # candidates the cosine missed.
        vec_pool = max(k * 3, 16)
        vec_hits = await self.recall(
            query,
            k=vec_pool,
            kinds=kinds,
            scopes=scopes,
            buckets=buckets,
            min_confidence=min_confidence,
            include_superseded=include_superseded,
            include_relations=False,
        )

        from xmclaw.memory.v2 import bm25
        if not bm25.is_available():
            return vec_hits[:k]

        # Build a corpus snapshot for BM25. Cap so 100K-fact stores
        # don't blow the budget — for typical XMclaw usage 5K is
        # plenty. Filters re-apply here so BM25 only ranks facts
        # the caller would have accepted from the vector path.
        clauses: list[str] = []
        if kinds:
            clauses.append(
                f"kind IN ({', '.join(repr(k_) for k_ in kinds)})",
            )
        if scopes:
            clauses.append(
                f"scope IN ({', '.join(repr(s) for s in scopes)})",
            )
        if buckets:
            clauses.append(
                f"bucket IN ({', '.join(repr(b) for b in buckets)})",
            )
        if min_confidence > 0:
            clauses.append(f"confidence >= {min_confidence}")
        if not include_superseded:
            clauses.append("superseded_by = ''")
        where = " AND ".join(clauses) if clauses else None
        # BM25 leg wall-clock budget — see method docstring. We run
        # the corpus scan + Python BM25 build under a single
        # ``wait_for`` so any slow step (LanceDB scan, tokenisation,
        # BM25Okapi.__init__) trips the same fast-fail back to
        # pure-vector. Without this, a 50K-fact store would spin
        # the user-turn critical path for tens of seconds.
        import asyncio as _asyncio
        import time as _t

        async def _bm25_leg() -> list[tuple[str, float]]:
            corpus_facts = await self._vec.search(
                None, where=where, limit=corpus_cap,
            )
            # Tokenisation + BM25 build is CPU-bound Python. Yield
            # the loop once before we burn the budget on it so the
            # ``wait_for`` deadline can actually fire if needed.
            await _asyncio.sleep(0)
            t_build = _t.perf_counter()
            idx = bm25.BM25Index(corpus_facts)
            result = idx.search(query, k=max(k * 3, 16))
            _log.debug(
                "memory_service.recall_hybrid.bm25_built "
                "corpus=%d elapsed_ms=%.0f",
                len(corpus_facts),
                (_t.perf_counter() - t_build) * 1000.0,
            )
            return result

        try:
            bm25_results = await _asyncio.wait_for(
                _bm25_leg(), timeout=bm25_deadline_s,
            )
        except _asyncio.TimeoutError:
            _log.info(
                "memory_service.recall_hybrid.bm25_deadline "
                "exceeded=%.2fs (falling back to pure vector)",
                bm25_deadline_s,
            )
            return vec_hits[:k]
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "memory_service.recall_hybrid.bm25_leg_failed "
                "err=%s (falling back to pure vector)", exc,
            )
            return vec_hits[:k]

        if not bm25_results:
            return vec_hits[:k]

        # Fuse. Vector score = (pool - rank) / pool, so top-of-pool
        # → 1.0 and bottom → ~0. BM25 score already normalised to
        # [0, 1] inside BM25Index.search.
        vec_pool_size = max(len(vec_hits), 1)
        fused: dict[str, float] = {}
        for rank, hit in enumerate(vec_hits):
            fid = hit.fact.id
            vec_score = (vec_pool_size - rank) / vec_pool_size
            fused[fid] = fused.get(fid, 0.0) + vec_weight * vec_score
        for fid, bm25_score in bm25_results:
            fused[fid] = fused.get(fid, 0.0) + bm25_weight * bm25_score

        # Resolve fact ids that BM25 found but the vector path didn't.
        by_id: dict[str, RecallHit] = {h.fact.id: h for h in vec_hits}
        missing_ids = [fid for fid in fused if fid not in by_id]
        if missing_ids:
            # Fetch in parallel; small batch.
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(20)

            async def _fetch(fid: str) -> tuple[str, Fact | None]:
                async with sem:
                    try:
                        return fid, await self._vec.get(fid)
                    except Exception:  # noqa: BLE001
                        return fid, None

            for fid, fact in await _asyncio.gather(
                *(_fetch(fid) for fid in missing_ids)
            ):
                if fact is not None:
                    by_id[fid] = RecallHit(
                        fact=fact, distance=0.0,
                        related_relations=[],
                    )

        # Sort fused descending, take top-K.
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        out: list[RecallHit] = []
        for fid, _score in ranked:
            hit = by_id.get(fid)
            if hit is not None:
                out.append(hit)
            if len(out) >= k:
                break
        return out

    async def sweep(
        self,
        *,
        ttl: dict[str, float | None] | None = None,
        max_items: dict[str, int | None] | None = None,
        max_bytes: dict[str, int | None] | None = None,
        protected_kinds: tuple[str, ...] = (
            "identity", "persona_manual",
        ),
    ) -> dict[str, Any]:
        """Phase 7.B.1 V1→V2 retention port.

        Three-axis retention sweep, run periodically by the daemon
        (see app_lifespan memory_v2 block). For each layer in
        {working, long_term}:

          * **TTL prune** — delete facts whose ``ts_last`` is older
            than ``ttl[layer]`` seconds ago. ``None`` disables the
            TTL axis for that layer (V1's default for ``long``).
          * **max_items cap** — if `count(layer)` exceeds the cap,
            delete the oldest non-protected rows until the cap is
            met.
          * **max_bytes cap** — sum ``len(text.encode('utf-8'))``
            across non-protected rows; drop oldest until the sum
            fits.

        ``procedural`` layer is exempt from all three axes —
        procedural facts (skills / persona) outlive aging policies
        by design (see FactLayer docstring).

        ``protected_kinds`` rows are exempt regardless of layer.
        Default protects identity + persona_manual (V1's
        equivalent of pinned_tags=['identity', 'user-profile']).

        Returns a summary dict::

            {
              "ttl_pruned":  {"working": N, "long_term": M},
              "cap_evicted": {"working": N, "long_term": M},
              "elapsed_ms":  float,
            }

        Best-effort: each axis is independent and isolated; one
        axis failing doesn't abort the others (logged).
        """
        import time as _t
        t0 = _t.perf_counter()
        ttl_map: dict[str, float | None] = dict(ttl or {})
        items_map: dict[str, int | None] = dict(max_items or {})
        bytes_map: dict[str, int | None] = dict(max_bytes or {})

        ttl_pruned: dict[str, int] = {}
        cap_evicted: dict[str, int] = {}
        prot_set: set[str] = set(protected_kinds)

        for layer in ("working", "long_term"):
            # Pull the whole layer once — we filter protected_kinds
            # Python-side rather than via SQL NOT IN, because the
            # in-memory backend's where parser doesn't support
            # NOT IN. Cheaper anyway (one scan covers both axes).
            try:
                listing = await self._vec.search(
                    None,
                    where=f"layer = '{layer}'",
                    limit=_MAINTENANCE_SCAN_LIMIT,
                )
                _maybe_warn_scan_truncated(
                    f"sweep.{layer}", len(listing),
                    _MAINTENANCE_SCAN_LIMIT,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "memory_service.sweep.scan_failed layer=%s err=%s",
                    layer, exc,
                )
                ttl_pruned[layer] = 0
                cap_evicted[layer] = 0
                continue

            # Filter out protected kinds — those are exempt from
            # every sweep axis.
            unprotected = [f for f in listing if f.kind not in prot_set]

            # ── Axis 1: TTL prune ────────────────────────────────
            ttl_v = ttl_map.get(layer)
            ttl_victim_ids: set[str] = set()
            if ttl_v is not None and ttl_v > 0:
                cutoff = _t.time() - float(ttl_v)
                ttl_victim_ids = {
                    f.id for f in unprotected if f.ts_last < cutoff
                }
            ttl_pruned[layer] = len(ttl_victim_ids)

            # ── Axis 2 + 3: cap-based eviction ───────────────────
            items_cap = items_map.get(layer)
            bytes_cap = bytes_map.get(layer)
            cap_victim_ids: set[str] = set()
            # Cap math runs over remaining (post-TTL) rows.
            remaining = [
                f for f in unprotected if f.id not in ttl_victim_ids
            ]
            remaining.sort(key=lambda f: f.ts_last)  # oldest-first

            if items_cap is not None and len(remaining) > items_cap:
                overflow = len(remaining) - items_cap
                for f in remaining[:overflow]:
                    cap_victim_ids.add(f.id)

            if bytes_cap is not None:
                budget = int(bytes_cap)
                keep: set[str] = set()
                for f in reversed(remaining):
                    nbytes = len((f.text or "").encode("utf-8"))
                    if nbytes <= budget:
                        keep.add(f.id)
                        budget -= nbytes
                    else:
                        break
                for f in remaining:
                    if f.id not in keep:
                        cap_victim_ids.add(f.id)

            # Single combined delete (TTL + cap victims).
            all_victims = ttl_victim_ids | cap_victim_ids
            if all_victims:
                try:
                    ids_list = ", ".join(f"'{vid}'" for vid in all_victims)
                    await self._vec.delete(f"id IN ({ids_list})")
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "memory_service.sweep.delete_failed layer=%s err=%s",
                        layer, exc,
                    )
                    # Reset counts since deletion didn't happen.
                    ttl_pruned[layer] = 0
                    cap_victim_ids = set()
            cap_evicted[layer] = len(cap_victim_ids)

        elapsed_ms = (_t.perf_counter() - t0) * 1000.0
        return {
            "ttl_pruned": ttl_pruned,
            "cap_evicted": cap_evicted,
            "elapsed_ms": round(elapsed_ms, 2),
        }

    async def delete(self, fact_id: str) -> bool:
        """Remove a Fact + all incident edges. Idempotent.

        Phase 7 V1→V2 shim: equivalent of
        ``UnifiedMemorySystem.delete(id)``. Returns True if a Fact
        with that id existed (and is now gone), False if the id was
        already absent.

        Best-effort cross-backend consistency: deletes the Fact row
        first, then sweeps neighbour edges in both directions. If the
        graph sweep fails, raises :class:`MemoryServiceWriteError`
        with ``indices_written=["vector"]`` and ``compensated=[]`` —
        the caller knows the fact body is gone but stale edges may
        remain (a graph janitor or full re-link can clean those up
        later; the vector deletion is the user-visible "fact is gone"
        signal which we don't roll back).
        """
        existed = (await self._vec.get(fact_id)) is not None
        if not existed:
            return False
        # Vector delete first — this is the user-visible "fact is
        # gone" operation. If it fails, we never touch the graph.
        try:
            await self._vec.delete(f"id = '{fact_id}'")
        except Exception as exc:  # noqa: BLE001
            raise MemoryServiceWriteError(
                f"delete({fact_id!r}): vector backend rejected delete",
                indices_written=[],
                compensated=[],
                cause=exc,
            ) from exc
        # Graph sweep: remove all edges where the fact is source OR
        # target. We walk neighbours (outgoing) + reverse-walk via
        # contradictions_of equivalents is not exposed, so we use
        # find_related which returns both directions for the
        # subgraph. Stale edges are non-fatal but should be reported.
        graph_errors: list[BaseException] = []
        try:
            pairs = await self._graph.neighbors(fact_id, max_hops=1)
            for rel, _target in pairs:
                try:
                    await self._graph.remove_relation(rel.id)
                except Exception as exc:  # noqa: BLE001
                    graph_errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            graph_errors.append(exc)
        if graph_errors:
            raise MemoryServiceWriteError(
                f"delete({fact_id!r}): vector row removed but "
                f"{len(graph_errors)} graph edge(s) failed to clean up",
                indices_written=["vector"],
                compensated=[],
                cause=graph_errors[0],
            )
        return True

    async def count(
        self,
        *,
        kinds: list[FactKindStr] | None = None,
        scopes: list[FactScopeStr] | None = None,
        buckets: list[str] | None = None,
        include_superseded: bool = False,
    ) -> int:
        clauses: list[str] = []
        if kinds:
            ks = ", ".join(f"'{k}'" for k in kinds)
            clauses.append(f"kind IN ({ks})")
        if scopes:
            ss = ", ".join(f"'{s}'" for s in scopes)
            clauses.append(f"scope IN ({ss})")
        if buckets:
            bs = ", ".join(f"'{b}'" for b in buckets)
            clauses.append(f"bucket IN ({bs})")
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
        query_embedding: list[float] | None = None,
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
        # 2026-05-29 perf fix: if the LanceDB backend has detected
        # on-disk corruption, short-circuit immediately.  Corrupted
        # LanceDB can hang the event loop for minutes on Windows
        # because the Rust I/O layer never yields back to asyncio.
        _vec = getattr(self, "_vec", None)
        _graph = getattr(self, "_graph", None)
        if getattr(_vec, "_corrupted", False) or getattr(_graph, "_corrupted", False):
            _log.info("memory_v2.render_for_prompt.short_circuit corrupted_backend")
            return ""
        # Epic #27 sweep #5 (2026-05-19): always-on + query-conditioned
        # recalls now fan out via asyncio.gather. Pre-fix this ran 4
        # sequential `recall()` calls — each one already serialized
        # through N neighbor lookups (the #4 path). Total cost was
        # 4 × the slowest recall. With both fixes the render_for_prompt
        # path drops from ~25s (real-world observed) to ~3s typical.
        #
        # Note on scope filters: ``scopes=["user"]`` / ``scopes=["project"]``
        # is correct semantics. Identity / preference facts that
        # belong cross-scope are routed via ``Fact.bucket`` to
        # persona MD files (see ``v2_renderer.BUCKET_TO_FILE``);
        # the prompt picks them up by reading the rendered files,
        # not by recall-dumping every scope. An earlier attempt
        # (G-08 follow-up first cut) widened scopes=None as a
        # workaround for a separate bucket-routing bug — that bug
        # is now fixed at the root (boot backfill + remember()
        # default infer) so this filter can stay tight.
        import asyncio as _asyncio
        user_t = self.recall(
            None, kinds=["preference", "identity", "correction"],
            scopes=["user"], k=20, include_relations=False,
        )
        project_t = self.recall(
            None, kinds=["project", "commitment"],
            scopes=["project"], k=20, include_relations=False,
        )
        decision_t = self.recall(
            None, kinds=["decision"], k=10, include_relations=False,
        )
        if query and query.strip():
            relevant_t = self.recall(
                query, k=k, include_relations=True,
            )
            user_facts, project_facts, decision_facts, relevant_hits = (
                await _asyncio.gather(user_t, project_t, decision_t, relevant_t)
            )
        else:
            user_facts, project_facts, decision_facts = (
                await _asyncio.gather(user_t, project_t, decision_t)
            )
            relevant_hits = []

        # Phase 8 ⑦ (2026-05-30): three-factor ranking (Generative
        # Agents). Re-order always-on facts by relevance + recency +
        # importance instead of relevance alone. Recency + importance
        # work even with NO query embedding (so always-on sections are
        # now ranked freshest-and-strongest-first rather than left in
        # the backend's ts_last DESC order).
        _now = time.time()
        _qvec = query_embedding if query_embedding else None
        _qnorm = sum(v * v for v in _qvec) ** 0.5 if _qvec else 0.0

        def _rank(hits: list[RecallHit]) -> list[RecallHit]:
            return sorted(
                hits,
                key=lambda h: _three_factor_score(
                    h.fact, query_vec=_qvec, query_norm=_qnorm, now=_now,
                ),
                reverse=True,
            )

        user_facts = _rank(user_facts)
        project_facts = _rank(project_facts)
        decision_facts = _rank(decision_facts)

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
            new_hits = _rank(
                [h for h in relevant_hits if h.fact.id not in seen_ids]
            )
            # Phase 8 ⑪ (2026-05-30): reinforcement. These query-relevant
            # facts were actually useful this turn → bump their ts_last
            # so recall-frequency feeds back into the recency score
            # (MemoryBank effect). Fire-and-forget; never blocks render.
            self._reinforce_facts([h.fact for h in new_hits])
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

    def _reinforce_facts(self, facts: list[Fact]) -> None:
        """Phase 8 ⑪: bump ts_last on facts that were just injected as
        query-relevant, so recall-frequency strengthens their recency
        score (MemoryBank, arXiv:2305.10250). Fire-and-forget — schedules
        a background upsert and returns immediately; never raises into
        the render path. Skips facts touched within
        ``REINFORCE_MIN_INTERVAL_S`` to avoid write amplification."""
        if not facts:
            return
        now = time.time()
        stale = [
            f for f in facts
            if (now - float(getattr(f, "ts_last", now) or now))
            >= REINFORCE_MIN_INTERVAL_S
        ]
        if not stale:
            return

        async def _bump() -> None:
            try:
                for f in stale:
                    f.ts_last = now
                await self._vec.upsert(stale)
                _log.debug(
                    "memory_service.reinforced n=%d", len(stale),
                )
            except Exception as exc:  # noqa: BLE001 — never break render
                _log.debug("memory_service.reinforce_failed err=%s", exc)

        try:
            import asyncio as _asyncio
            _asyncio.get_running_loop().create_task(_bump())
        except RuntimeError:
            # No running loop (sync test context) — skip silently;
            # reinforcement is a best-effort optimisation, not a
            # correctness requirement.
            pass

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
            None, where=where, limit=_MAINTENANCE_SCAN_LIMIT,
        )
        _maybe_warn_scan_truncated(
            "deduplicate", len(all_facts), _MAINTENANCE_SCAN_LIMIT,
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
        all_facts = await self._vec.search(
            None, where=None, limit=_MAINTENANCE_SCAN_LIMIT,
        )
        _maybe_warn_scan_truncated(
            "co_occurrence_backfill",
            len(all_facts), _MAINTENANCE_SCAN_LIMIT,
        )
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

    # ── Wave-32+ broader SAME_TOPIC backfill ────────────────────

    async def relink_same_topic(
        self, *, dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-run the SAME_TOPIC auto-link logic over every existing
        fact using the WAVE-32+ broader rules (drop same-kind
        restriction, raise neighbor limit, add shared-entity bridge).

        Use case: the user opens the graph view and sees orphan
        clusters that obviously belong together — they were written
        BEFORE the Wave-32+ rules went live, so the original scan
        missed the bridges. One POST to this endpoint rebuilds the
        edges over the existing store.

        Returns ``{scanned, edges_added, edges_skipped, dry_run}``.
        """
        all_facts = await self._vec.search(
            None, where=None, limit=_MAINTENANCE_SCAN_LIMIT,
        )
        _maybe_warn_scan_truncated(
            "same_topic_relink", len(all_facts), _MAINTENANCE_SCAN_LIMIT,
        )
        # Index by id so the inner pass can look up vectors cheaply.
        all_facts = [f for f in all_facts if not f.superseded_by]
        if not all_facts:
            return {"scanned": 0, "edges_added": 0, "edges_skipped": 0, "dry_run": dry_run}

        # Pre-extract entity tokens for the bridge pass — done once
        # so the O(N^2) inner loop just does set intersection.
        token_index: dict[str, set[str]] = {
            f.id: _extract_entity_tokens(f.text) for f in all_facts
        }

        added = 0
        skipped = 0
        seen_pairs: set[frozenset[str]] = set()
        for fact in all_facts:
            if fact.embedding is None:
                continue
            # Vec-distance pass with the new broader rules.
            try:
                nearby = await self._vec.search(
                    list(fact.embedding),
                    where=f"id != '{fact.id}' AND superseded_by = ''",
                    limit=10,
                )
            except Exception:  # noqa: BLE001
                continue
            vec_matches: set[str] = set()
            for cand in nearby:
                if cand.embedding is None:
                    continue
                d = _cosine_distance(fact.embedding, cand.embedding)
                if d <= SAME_TOPIC_DISTANCE_THRESHOLD:
                    vec_matches.add(cand.id)
            # Shared-entity bridge pass.
            entity_matches: set[str] = set()
            my_tokens = token_index.get(fact.id, set())
            if my_tokens:
                for cand in nearby:
                    if cand.id in vec_matches:
                        continue
                    if token_index.get(cand.id, set()) & my_tokens:
                        entity_matches.add(cand.id)
            # Pairwise edges (symmetric) — skip pairs we already
            # processed from the other side.
            for target in vec_matches | entity_matches:
                pair = frozenset({fact.id, target})
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if dry_run:
                    added += 2  # would emit both directions
                    continue
                for src, dst in ((fact.id, target), (target, fact.id)):
                    rid = Relation.compute_id(
                        source_fact_id=src,
                        target_fact_id=dst,
                        relation=RelationKind.SAME_TOPIC,
                    )
                    try:
                        await self._graph.add_relation(Relation(
                            id=rid,
                            source_fact_id=src,
                            target_fact_id=dst,
                            relation=RelationKind.SAME_TOPIC.value,
                            strength=0.6,
                            auto_extracted=True,
                        ))
                        added += 1
                    except Exception:  # noqa: BLE001
                        skipped += 1
        return {
            "scanned": len(all_facts),
            "edges_added": added,
            "edges_skipped": skipped,
            "dry_run": dry_run,
        }

    # ── Wave-32+ LLM topic layer (delegates to llm_topic.py) ────

    async def llm_topic_refine(
        self,
        llm: Any,
        *,
        budget: int = 20,
    ) -> dict[str, Any]:
        """Ask an LLM to judge borderline same-topic pairs (vec
        distance just over the cluster threshold). See
        :mod:`xmclaw.memory.v2.llm_topic` for details."""
        from xmclaw.memory.v2.llm_topic import refine_same_topic
        return await refine_same_topic(self, llm, budget=budget)

    async def llm_topic_name(
        self,
        llm: Any,
        *,
        budget: int = 5,
    ) -> dict[str, Any]:
        """Find SAME_TOPIC clusters of 3+ facts without an existing
        topic node, ask the LLM to name them, write topic fact +
        PART_OF edges. See :mod:`xmclaw.memory.v2.llm_topic`."""
        from xmclaw.memory.v2.llm_topic import name_clusters
        return await name_clusters(self, llm, budget=budget)

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
        all_facts = await self._vec.search(
            None, where=None, limit=_MAINTENANCE_SCAN_LIMIT,
        )
        _maybe_warn_scan_truncated(
            "clear_stale_contradicts",
            len(all_facts), _MAINTENANCE_SCAN_LIMIT,
        )
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
