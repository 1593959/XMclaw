"""Backend Protocols — VectorBackend + GraphBackend (Phase 1a).

Two async Protocols that abstract the storage layer away from the
business logic in ``memory_service`` (Phase 2).

Why Protocol instead of ABC: stuctural typing means we can drop in a
test stub or a totally different backend (SurrealDB, Qdrant, ...)
without changing the upstream type annotations — just match the
shape. ``runtime_checkable`` lets ``isinstance`` work for sanity
checks at boot.

The Protocols are deliberately MINIMAL. They expose only what
``memory_service`` actually needs. Each concrete backend may have
richer APIs internally; callers must go through the Protocol surface.

Concrete implementations:
    - ``InMemoryVectorBackend``  (this module's test stub)
    - ``LanceDBVectorBackend``   (production, lazy-imports lancedb)
    - ``InMemoryGraphBackend``   (test stub)
    - ``LanceDBGraphBackend``    (production)
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from xmclaw.memory.v2.models import Fact, Relation


# ── Vector backend ────────────────────────────────────────────────


@runtime_checkable
class VectorBackend(Protocol):
    """Storage abstraction for L1 facts + KNN-with-filter search.

    All methods are async because the production backend (LanceDB)
    is async; sync backends can wrap with ``asyncio.to_thread``.

    Contract:
        - ``upsert``: by ``Fact.id`` — same id ⇒ replace + bump
          ``evidence_count``. Embeddings are stored verbatim; the
          caller is responsible for embedding via EmbeddingService.
        - ``search``: KNN by embedding OR keyword (BM25 fallback);
          ``where`` is a SQL-like filter string evaluated AGAINST
          the underlying schema columns. Empty list = no hit.
        - ``delete``: by ``where`` clause — explicit DELETE FROM ...
        - ``count``: cheap row count, optionally filtered.
        - ``get``: by exact id.
    """

    async def upsert(self, records: list[Fact]) -> int:
        """Insert or replace ``records`` keyed by ``Fact.id``.

        Returns the number of rows touched (inserted + replaced).
        On duplicate id the implementation must MERGE semantics:
        keep highest confidence, sum evidence_count, advance
        ts_last. See ``_merge_fact`` in InMemoryVectorBackend for
        the reference semantics; LanceDB does the same via
        merge_insert + update_all.
        """
        ...

    async def search(
        self,
        query: list[float] | str | None = None,
        *,
        where: str | None = None,
        limit: int = 8,
    ) -> list[Fact]:
        """KNN (if ``query`` is a vector), keyword search (if str),
        or pure-filter listing (if both None).

        ``where`` syntax: SQL-flavoured against Fact columns
        (``kind``, ``scope``, ``confidence``, ``layer``,
        ``evidence_count``, ``ts_last``). InMemory implements a
        small subset; LanceDB hands it through.
        """
        ...

    async def delete(self, where: str) -> int:
        """Delete rows matching ``where``. Returns count deleted."""
        ...

    async def count(self, where: str | None = None) -> int:
        """Cheap COUNT(*) with optional filter."""
        ...

    async def get(self, fact_id: str) -> Fact | None:
        """Exact-id fetch, None if absent."""
        ...

    async def close(self) -> None:
        """Release resources (file handles, connections). Idempotent."""
        ...


# ── Graph backend ─────────────────────────────────────────────────


@runtime_checkable
class GraphBackend(Protocol):
    """Storage abstraction for L1 typed edges between facts.

    Mirrors VectorBackend's shape:
        - ``add_relation``: idempotent upsert by Relation.id
        - ``neighbors``: 1-hop fan-out from a fact
        - ``find_related``: subgraph extraction for UI viz
        - ``contradictions_of``: convenience for the
          CONTRADICTS-aware prompt injection

    For ``CAUSED_BY`` edges that point back into L0 events.db, the
    target_fact_id may be ``f"event:{event_id}"`` — backends treat
    that as opaque (no JOIN, no validation).
    """

    async def add_relation(self, rel: Relation) -> None:
        """Upsert one edge by Relation.id."""
        ...

    async def add_relations(self, rels: list[Relation]) -> int:
        """Batch upsert. Returns count touched."""
        ...

    async def remove_relation(self, rel_id: str) -> None:
        """Idempotent delete by Relation.id."""
        ...

    async def neighbors(
        self,
        fact_id: str,
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> list[tuple[Relation, str]]:
        """Return outgoing edges from ``fact_id`` (1+ hops).

        Each element is ``(edge, target_fact_id)``. Pure graph walk —
        does NOT fetch the target Fact from the vector store; the
        caller composes both backends if it needs Fact bodies.
        """
        ...

    async def find_related(
        self,
        fact_ids: list[str],
        *,
        max_hops: int = 1,
        relation_types: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Subgraph extraction for UI viz.

        Returns ``{"nodes": [fact_id, ...], "edges": [Relation, ...]}``
        in a shape vis-network / Cytoscape can consume directly. The
        node list is just IDs; the caller hydrates Fact bodies via
        VectorBackend.get if needed.

        ``limit`` is on edges (sorted by strength DESC) to keep the
        UI snappy on hot facts.
        """
        ...

    async def contradictions_of(self, fact_id: str) -> list[str]:
        """Convenience: return target_fact_ids reachable from
        ``fact_id`` via a ``CONTRADICTS`` edge. Empty when none.

        Used by recall() to tag prompts with "fact-9 contradicts
        this — don't use fact-9" hints (§8.4 of design doc).
        """
        ...

    async def close(self) -> None:
        """Release resources. Idempotent."""
        ...


__all__ = [
    "GraphBackend",
    "VectorBackend",
]
