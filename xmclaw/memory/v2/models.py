"""L1 data models — Fact + Relation (Phase 1a).

Two dataclasses + four enums. Pure data, zero I/O. Backends in
``backend_*`` modules consume / produce these shapes.

Design rationale: see ``docs/MEMORY_EVOLUTION_REDESIGN.md`` §4.

  * **Fact** = one durable assertion ("用户喜欢简短回复"). The 7
    ``FactKind`` values cover what the agent actually needs to
    remember at L1 (preference / decision / identity / commitment /
    correction / project / episode).
  * **Relation** = a typed directed edge between two facts. 6
    ``RelationKind`` values — see the design doc §16.8.5 for the
    intent of each.

Deterministic id derivation lives on each dataclass as a classmethod
so writers don't have to invent ids inline. ``Fact.compute_id`` and
``Relation.compute_id`` produce stable string ids — same content +
same kind/scope ⇒ same id ⇒ idempotent upsert downstream.

We DON'T import lancedb / pyarrow here. Models stay backend-neutral
so unit tests run without optional deps installed.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


# ── Enums (str-backed so they JSON-serialize cleanly) ─────────────


class FactKind(str, Enum):
    """L1 fact taxonomy.

    Eight kinds keep the schema strict without becoming a free-form
    bag. Each kind has a different lifecycle + auto-extraction
    trigger (see §4.4 of design doc):

    - ``preference`` — long-lived user preference
    - ``decision``   — committed choice that shapes future work
    - ``identity``   — slow-changing self / env fact about the user
    - ``commitment`` — open promise (auto-expire when fulfilled)
    - ``correction`` — explicit "don't do X" / "you got it wrong"
    - ``project``    — business / domain parameters (URLs, accounts,
                       goals, KPIs — the things daemon hook intercepts)
    - ``episode``    — a discrete successful problem-solving event;
                       feeds L2 experience distillation
    - ``lesson``     — workflow / tool-quirk / failure-mode / value /
                       rule extracted by ExtractLessonsHook. Lives
                       here so the dedup pipeline (write-time merge,
                       bulk consolidation, SUPERSEDES graph) covers
                       it the same way as the other seven kinds.
                       Legacy: previously only stored in
                       ``memory.db`` (SqliteVecMemory kind=lesson)
                       and persona MD files; v2 facts now indexes
                       them too.
    """

    PREFERENCE = "preference"
    DECISION = "decision"
    IDENTITY = "identity"
    COMMITMENT = "commitment"
    CORRECTION = "correction"
    PROJECT = "project"
    EPISODE = "episode"
    LESSON = "lesson"


class FactScope(str, Enum):
    """Visibility / lifetime scope for a Fact.

    - ``user``    — about the human, persists across projects
    - ``project`` — about the current project (XMclaw, customer X, etc.)
    - ``session`` — only relevant to the current conversation;
                    DreamCompactor expires these
    """

    USER = "user"
    PROJECT = "project"
    SESSION = "session"


class FactLayer(str, Enum):
    """Tiered storage layer.

    - ``working`` — fresh, evidence_count < 3, may be evicted on cap
    - ``long_term`` — promoted, durable

    Promotion criterion: ``evidence_count >= 3`` OR explicit user
    "记住" command. Demotion criterion: 30 days no retrieval +
    layer=working.
    """

    WORKING = "working"
    LONG_TERM = "long_term"


class RelationKind(str, Enum):
    """L1 directed edge taxonomy.

    Six kinds map exactly to the relations that auto-fire from
    business logic (§16.8.5 of design doc):

    - ``CONTRADICTS``  — two facts logically conflict (auto from
                          remember() collision check)
    - ``SUPERSEDES``   — newer fact replaces older one of same id
    - ``CAUSED_BY``    — fact links back to L0 event that produced it
    - ``PART_OF``      — fact is a component of a larger fact / episode
    - ``REFERS_TO``    — fact mentions an entity / other fact (LLM extract)
    - ``SAME_TOPIC``   — vec distance < 0.2 cluster (periodic scan)
    """

    CONTRADICTS = "CONTRADICTS"
    SUPERSEDES = "SUPERSEDES"
    CAUSED_BY = "CAUSED_BY"
    PART_OF = "PART_OF"
    REFERS_TO = "REFERS_TO"
    SAME_TOPIC = "SAME_TOPIC"


# Type aliases — accept either enum instance or its str value at
# write sites. Backends normalise to the str (Lance schema can't hold
# Python enums).
FactKindStr = Literal[
    "preference", "decision", "identity",
    "commitment", "correction", "project", "episode",
    "lesson",
]
FactScopeStr = Literal["user", "project", "session"]
FactLayerStr = Literal["working", "long_term"]
RelationKindStr = Literal[
    "CONTRADICTS", "SUPERSEDES", "CAUSED_BY",
    "PART_OF", "REFERS_TO", "SAME_TOPIC",
]


# ── Fact ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class Fact:
    """One L1 fact.

    Mutable on purpose: ``evidence_count`` / ``ts_last`` / ``confidence``
    are upserted in place by ``remember()`` when the same content
    arrives again.
    """

    id: str
    kind: FactKindStr
    scope: FactScopeStr
    text: str
    confidence: float = 0.8
    evidence_count: int = 1
    embedding: tuple[float, ...] | None = None
    source_event_id: str | None = None
    contradicts: tuple[str, ...] = ()
    superseded_by: str | None = None
    layer: FactLayerStr = "working"
    ts_first: float = field(default_factory=time.time)
    ts_last: float = field(default_factory=time.time)

    # ── id derivation ────────────────────────────────────────────

    @staticmethod
    def compute_id(
        *, kind: FactKind | FactKindStr, scope: FactScope | FactScopeStr,
        text: str,
    ) -> str:
        """Deterministic id: ``f"{kind}:{scope}:{hash(text)[:12]}"``.

        Same (kind, scope, text) ⇒ same id ⇒ upsert idempotent.

        Whitespace normalised (strip + single-space) so trivial
        rewordings of the same fact collide. NOT semantic dedup —
        that's the vec-distance-< 0.2 SAME_TOPIC relation, separate
        path.
        """
        k = kind.value if isinstance(kind, FactKind) else str(kind)
        s = scope.value if isinstance(scope, FactScope) else str(scope)
        normalised = " ".join(text.split())
        h = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]
        return f"{k}:{s}:{h}"

    # ── normalisation helpers ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict for serialization (events bus / WS / JSON)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "text": self.text,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "embedding": list(self.embedding) if self.embedding else None,
            "source_event_id": self.source_event_id,
            "contradicts": list(self.contradicts),
            "superseded_by": self.superseded_by,
            "layer": self.layer,
            "ts_first": self.ts_first,
            "ts_last": self.ts_last,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fact":
        emb = d.get("embedding")
        return cls(
            id=str(d["id"]),
            kind=d["kind"],
            scope=d["scope"],
            text=str(d["text"]),
            confidence=float(d.get("confidence", 0.8)),
            evidence_count=int(d.get("evidence_count", 1)),
            embedding=tuple(emb) if emb else None,
            source_event_id=d.get("source_event_id"),
            contradicts=tuple(d.get("contradicts") or ()),
            superseded_by=d.get("superseded_by"),
            layer=d.get("layer", "working"),
            ts_first=float(d.get("ts_first") or time.time()),
            ts_last=float(d.get("ts_last") or time.time()),
        )


# ── Relation ─────────────────────────────────────────────────────


@dataclass(slots=True)
class Relation:
    """A directed L1 edge between two facts (or fact ↔ L0 event).

    For ``CAUSED_BY``: ``target_fact_id`` may be the ``event:<id>``
    pseudo-id pointing back into events.db; the GraphBackend treats
    it as opaque so the storage layer doesn't need to know about L0.
    """

    id: str
    source_fact_id: str
    target_fact_id: str
    relation: RelationKindStr
    strength: float = 1.0
    auto_extracted: bool = True
    ts: float = field(default_factory=time.time)

    @staticmethod
    def compute_id(
        *, source_fact_id: str, target_fact_id: str,
        relation: RelationKind | RelationKindStr,
    ) -> str:
        """Deterministic id: ``f"{relation}:{source}->{target}"``.

        Two facts can't have two different edges of the same kind —
        if they do, the second write upserts the strength of the
        first. That's what we want (idempotent re-extraction doesn't
        bloat the table).
        """
        r = relation.value if isinstance(relation, RelationKind) else str(relation)
        return f"{r}:{source_fact_id}->{target_fact_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_fact_id": self.source_fact_id,
            "target_fact_id": self.target_fact_id,
            "relation": self.relation,
            "strength": self.strength,
            "auto_extracted": self.auto_extracted,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Relation":
        return cls(
            id=str(d["id"]),
            source_fact_id=str(d["source_fact_id"]),
            target_fact_id=str(d["target_fact_id"]),
            relation=d["relation"],
            strength=float(d.get("strength", 1.0)),
            auto_extracted=bool(d.get("auto_extracted", True)),
            ts=float(d.get("ts") or time.time()),
        )
