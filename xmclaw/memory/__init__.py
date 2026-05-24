"""User-facing memory namespace — V2 only (Phase 7.B.4, 2026-05-24).

Re-exports the V2 ``MemoryService`` + supporting models. The legacy
V1 namespace (``UnifiedMemorySystem``, ``MemoryExtractor``,
``MemoryEntry``, ``TimeRange``, ``mint_unified_id``,
``UnifiedWriteError``, ``ExtractedFact``, ``TriggerKind``) was
physically deleted in this commit; callers that still need a
typed fact API import from ``xmclaw.memory.v2`` directly.

History:
  * Pre-Wave 27: ``xmclaw.memory.unified`` (UnifiedMemorySystem) was
    the single entry point — multi-axis facade over sqlite_vec +
    MemoryGraph + temporal index.
  * Wave 27 (2026-05): ``xmclaw.memory.v2`` shipped alongside; both
    coexisted with a documented "Phase 5 swap" that never happened.
  * Phase 7 (2026-05-23/24): V1 retired and deleted. ``MemoryService``
    is now the only API surface; LanceDB is the only backend for
    L1 facts. See JARVIS_PLAN §Phase 7 for the full timeline.

Workspace indexing (``MemoryFileIndexer`` writing file_chunk /
code_chunk into sqlite_vec) is unrelated to this namespace and
continues to use ``xmclaw.providers.memory.sqlite_vec`` directly.
"""
from __future__ import annotations

from xmclaw.memory.v2 import (
    Fact,
    FactKind,
    FactKindStr,
    FactLayer,
    FactLayerStr,
    FactScope,
    FactScopeStr,
    LLMCandidate,
    LLMFactExtractor,
    MemoryService,
    MemoryServiceWriteError,
    RecallHit,
    Relation,
    RelationKind,
    RelationKindStr,
    legacy_node_type_to_kind,
)

__all__ = [
    "Fact",
    "FactKind",
    "FactKindStr",
    "FactLayer",
    "FactLayerStr",
    "FactScope",
    "FactScopeStr",
    "LLMCandidate",
    "LLMFactExtractor",
    "MemoryService",
    "MemoryServiceWriteError",
    "RecallHit",
    "Relation",
    "RelationKind",
    "RelationKindStr",
    "legacy_node_type_to_kind",
]
