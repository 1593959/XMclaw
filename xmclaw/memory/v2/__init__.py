"""Memory v2 — Phase 1a scaffolding (Wave 27).

Implements the L1 facts + relations layer of the four-layer Memory ×
Evolution pipeline described in ``docs/MEMORY_EVOLUTION_REDESIGN.md``:

    L0  events  → L1  facts  → L2  experience  → L3  skills

This module owns L1 only. L0 is ``events.db`` (unchanged). L2/L3 live
in separate modules added later in the phase plan.

Public surface (Phase 1a):
    Fact, Relation, FactKind, FactScope, RelationKind, FactLayer
    VectorBackend, GraphBackend (Protocols)
    InMemoryVectorBackend, InMemoryGraphBackend (test stubs)
    LanceDBVectorBackend, LanceDBGraphBackend (production)

Higher-level ``memory_service.remember/recall/relate/neighbors`` (Phase
2) and the daemon entry hook (Phase 3) will land in later commits.

The old ``xmclaw.memory`` namespace (UnifiedMemorySystem,
MemoryExtractor, ...) is kept untouched during migration. v2 lives in
this subpackage so both can coexist until Phase 5 swap.
"""
from __future__ import annotations

from xmclaw.memory.v2.backend import GraphBackend, VectorBackend
from xmclaw.memory.v2.backend_inmemory import (
    InMemoryGraphBackend,
    InMemoryVectorBackend,
)
from xmclaw.memory.v2.embedding import (
    EmbeddingFailure,
    EmbeddingService,
    StubEmbedder,
    build_embedding_service,
)
from xmclaw.memory.v2.key_info_extractor import (
    ExtractedKey,
    extract_and_remember,
    extract_keys,
)
from xmclaw.memory.v2.llm_extractor import (
    LLMFactExtractor,
    llm_extract_and_remember,
)
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
from xmclaw.memory.v2.service import (
    CONTRADICTS_DISTANCE_THRESHOLD,
    LONG_TERM_PROMOTE_THRESHOLD,
    MemoryService,
    RecallHit,
    SAME_TOPIC_DISTANCE_THRESHOLD,
)

# LanceDB backends lazy-imported via helper so importing
# xmclaw.memory.v2 doesn't require lancedb to be installed.

def get_lancedb_vector_backend(*args, **kwargs):
    """Lazy factory — only imports lancedb when called."""
    from xmclaw.memory.v2.backend_lancedb import LanceDBVectorBackend
    return LanceDBVectorBackend(*args, **kwargs)


def get_lancedb_graph_backend(*args, **kwargs):
    """Lazy factory — only imports lancedb when called."""
    from xmclaw.memory.v2.backend_lancedb import LanceDBGraphBackend
    return LanceDBGraphBackend(*args, **kwargs)


__all__ = [
    "CONTRADICTS_DISTANCE_THRESHOLD",
    "EmbeddingFailure",
    "EmbeddingService",
    "ExtractedKey",
    "LLMFactExtractor",
    "Fact",
    "FactKind",
    "FactKindStr",
    "FactLayer",
    "FactLayerStr",
    "FactScope",
    "FactScopeStr",
    "GraphBackend",
    "InMemoryGraphBackend",
    "InMemoryVectorBackend",
    "LONG_TERM_PROMOTE_THRESHOLD",
    "MemoryService",
    "RecallHit",
    "Relation",
    "RelationKind",
    "RelationKindStr",
    "SAME_TOPIC_DISTANCE_THRESHOLD",
    "StubEmbedder",
    "VectorBackend",
    "build_embedding_service",
    "extract_and_remember",
    "extract_keys",
    "llm_extract_and_remember",
    "get_lancedb_graph_backend",
    "get_lancedb_vector_backend",
]
