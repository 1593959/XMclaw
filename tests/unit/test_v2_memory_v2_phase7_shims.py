"""Phase 7.A.2 — V1→V2 shim API tests.

Covers the four low-risk P0 shims from
``docs/audit/AUDIT_2026-05-23_phase7_memory_v1_callsites.md``:

  * FactLayer.PROCEDURAL enum value (#2)
  * MemoryService.delete(fact_id) (#3)
  * MemoryServiceWriteError (#5)
  * legacy_node_type_to_kind() mapping (#6)

The three more-complex shims (time_range / query_layer /
extract_candidates) land in a later commit with their own tests.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    FactKind,
    FactLayer,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    MemoryServiceWriteError,
    Relation,
    RelationKind,
    StubEmbedder,
    legacy_node_type_to_kind,
)


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ── Shim 2: FactLayer.PROCEDURAL ─────────────────────────────────


def test_fact_layer_has_procedural_value() -> None:
    """Procedural enum value exists + serializes to the expected str."""
    assert FactLayer.PROCEDURAL.value == "procedural"
    # str-Enum behaviour: equality against the literal string.
    assert FactLayer.PROCEDURAL == "procedural"


@pytest.mark.asyncio
async def test_remember_with_procedural_layer() -> None:
    """remember(layer='procedural') stores the row with that layer
    string verbatim (no silent coercion to working / long_term)."""
    svc = _make_service()
    fact = await svc.remember(
        "skill: summarize_meeting",
        kind=FactKind.LESSON,
        scope=FactScope.PROJECT,
        layer="procedural",
    )
    assert fact.layer == "procedural"
    # Round-trip through the backend to make sure storage preserves it.
    refetched = await svc.get_fact(fact.id)
    assert refetched is not None
    assert refetched.layer == "procedural"


# ── Shim 5: MemoryServiceWriteError ──────────────────────────────


def test_write_error_carries_compensation_surface() -> None:
    """Mirror the V1 UnifiedWriteError contract: indices_written
    minus compensated identifies the dirty surface."""
    cause = RuntimeError("graph edge failed")
    err = MemoryServiceWriteError(
        "delete failed mid-fanout",
        indices_written=["vector", "graph"],
        compensated=["vector"],
        cause=cause,
    )
    assert str(err) == "delete failed mid-fanout"
    assert err.indices_written == ["vector", "graph"]
    assert err.compensated == ["vector"]
    # The dirty surface = indices_written − compensated.
    dirty = set(err.indices_written) - set(err.compensated)
    assert dirty == {"graph"}
    assert err.cause is cause


def test_write_error_defaults_to_empty_lists() -> None:
    err = MemoryServiceWriteError("boom")
    assert err.indices_written == []
    assert err.compensated == []
    assert err.cause is None


# ── Shim 6: legacy_node_type_to_kind() ───────────────────────────


@pytest.mark.parametrize(
    "legacy, expected",
    [
        ("preference", "preference"),
        ("decision", "decision"),
        ("identity", "identity"),
        ("PREFERENCE", "preference"),  # case-insensitive
        # Legacy V1 buckets without strict V2 equivalent → "lesson"
        ("observation", "lesson"),
        ("rule", "lesson"),
        ("workflow", "lesson"),
        ("failure_mode", "lesson"),
        ("fact", "lesson"),
        # Unknown / weird → "lesson" (forgiving default).
        ("totally_made_up", "lesson"),
        ("", "lesson"),
        (None, "lesson"),
    ],
)
def test_legacy_node_type_to_kind(legacy: str | None, expected: str) -> None:
    assert legacy_node_type_to_kind(legacy) == expected


# ── Shim 3: MemoryService.delete() ───────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_existing_fact() -> None:
    svc = _make_service()
    fact = await svc.remember(
        "用户喜欢简短回复",
        kind=FactKind.PREFERENCE,
        scope=FactScope.USER,
    )
    assert (await svc.get_fact(fact.id)) is not None

    deleted = await svc.delete(fact.id)
    assert deleted is True
    assert (await svc.get_fact(fact.id)) is None


@pytest.mark.asyncio
async def test_delete_returns_false_for_missing_fact() -> None:
    svc = _make_service()
    deleted = await svc.delete("nonexistent:user:0123456789ab")
    assert deleted is False


@pytest.mark.asyncio
async def test_delete_sweeps_incident_edges() -> None:
    """Deleting a fact removes all its outgoing edges so the graph
    doesn't keep dangling references."""
    svc = _make_service()
    a = await svc.remember(
        "用户喜欢 Python",
        kind=FactKind.PREFERENCE, scope=FactScope.USER,
    )
    b = await svc.remember(
        "用户喜欢 Rust",
        kind=FactKind.PREFERENCE, scope=FactScope.USER,
    )
    # Manually relate them so we have an edge to sweep.
    await svc.relate(
        source_fact_id=a.id, target_fact_id=b.id,
        kind=RelationKind.SAME_TOPIC, strength=0.5,
    )
    # Verify edge exists before delete.
    pre = await svc._graph.neighbors(a.id, max_hops=1)
    assert any(rel.target_fact_id == b.id for rel, _ in pre)

    await svc.delete(a.id)

    post = await svc._graph.neighbors(a.id, max_hops=1)
    assert post == []


@pytest.mark.asyncio
async def test_delete_raises_write_error_when_graph_sweep_fails() -> None:
    """When the graph backend rejects edge cleanup AFTER the vector
    row is gone, we surface MemoryServiceWriteError with the
    expected compensation surface so the operator can clean up."""
    svc = _make_service()
    fact = await svc.remember(
        "boom",
        kind=FactKind.LESSON, scope=FactScope.PROJECT,
    )

    # Inject a graph that pretends to have one edge then refuses to
    # remove it. Patch the methods directly on the instance.
    async def _fake_neighbors(fact_id: str, **_kw):
        # Return one fake edge so the delete loop tries to remove it.
        rel = Relation(
            id="SAME_TOPIC:a->b",
            source_fact_id=fact.id,
            target_fact_id="b",
            relation="SAME_TOPIC",
        )
        return [(rel, "b")]

    async def _refuse(_rel_id: str) -> None:
        raise RuntimeError("graph offline")

    svc._graph.neighbors = _fake_neighbors  # type: ignore[assignment]
    svc._graph.remove_relation = _refuse  # type: ignore[assignment]

    with pytest.raises(MemoryServiceWriteError) as ei:
        await svc.delete(fact.id)
    assert ei.value.indices_written == ["vector"]
    assert ei.value.compensated == []
    # Vector row is still gone (we don't roll back the user-visible
    # "fact is gone" signal).
    assert (await svc.get_fact(fact.id)) is None
