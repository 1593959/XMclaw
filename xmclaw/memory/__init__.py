"""Unified memory system facade — ``xmclaw-architecture-redesign.md`` §3.3.

Per the updated design, memory has TWO orthogonal axes:

  * **Storage layer** (functional): working / short-term / long-term /
    procedural — decides where data lives + how it ages.
  * **Index** (query-dimensional): vector (semantic similarity) /
    graph (relational traversal) / temporal (time range) — decides
    how data is found.

Layered storage existed pre-this-module (sqlite_vec for short-term +
long-term semantic, MemoryGraph for relations, skill_registry for
procedural). What was missing: a SINGLE entry point exposing the
combined query surface — ``query(semantic, relation, temporal,
layer, limit)`` — so callers don't have to know which underlying
provider answers each axis.

This module ships that facade. Underlying providers stay where they
are (no migration of data).
"""
from __future__ import annotations

from xmclaw.memory.unified import (
    MemoryEntry,
    TimeRange,
    UnifiedMemorySystem,
)

__all__ = [
    "MemoryEntry",
    "TimeRange",
    "UnifiedMemorySystem",
]
