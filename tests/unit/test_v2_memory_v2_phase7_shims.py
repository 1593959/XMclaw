"""Phase 7.A.2 — V1→V2 shim API tests.

Covers six of the seven P0 shims from
``docs/audit/AUDIT_2026-05-23_phase7_memory_v1_callsites.md``:

  * recall(time_range=...) (#1)
  * FactLayer.PROCEDURAL enum value (#2)
  * MemoryService.delete(fact_id) (#3)
  * MemoryServiceWriteError (#5)
  * legacy_node_type_to_kind() mapping (#6)
  * LLMFactExtractor.extract_candidates + LLMCandidate (#7)

Shim #4 (query_layer) was dropped — reflection_cycle migration uses
``recall(only_layer="working", time_range=...)`` instead of adding a
short_term layer (decision: keep V2's 2-layer model clean).
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
    LLMCandidate,
    LLMFactExtractor,
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


# ── Shim 1: recall(time_range=...) ───────────────────────────────


@pytest.mark.asyncio
async def test_recall_time_range_window() -> None:
    """Bounded (start, end) window returns only facts whose ts_last
    sits inside the inclusive interval."""
    import time as _t
    svc = _make_service()
    # Make 3 facts with monotonically increasing timestamps. We can't
    # set ts_last via remember() so we patch in place after upsert.
    old_fact = await svc.remember("old", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    mid_fact = await svc.remember("mid", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    new_fact = await svc.remember("new", kind=FactKind.LESSON, scope=FactScope.PROJECT)

    now = _t.time()
    # Stamp directly in the in-memory backend so the where clause has
    # something to filter on.
    backend = svc._vec
    backend._rows[old_fact.id].ts_last = now - 100.0  # type: ignore[attr-defined]
    backend._rows[mid_fact.id].ts_last = now - 50.0  # type: ignore[attr-defined]
    backend._rows[new_fact.id].ts_last = now  # type: ignore[attr-defined]

    # Window: last 75s → mid + new, NOT old.
    hits = await svc.recall(
        time_range=(now - 75.0, now + 10.0),
        k=20, keyword_only=True, min_confidence=0.0,
    )
    ids = {h.fact.id for h in hits}
    assert mid_fact.id in ids
    assert new_fact.id in ids
    assert old_fact.id not in ids


@pytest.mark.asyncio
async def test_recall_time_range_since_only() -> None:
    """(start, None) = open-ended since-start."""
    import time as _t
    svc = _make_service()
    f1 = await svc.remember("old1", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    f2 = await svc.remember("new2", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    now = _t.time()
    svc._vec._rows[f1.id].ts_last = now - 100.0  # type: ignore[attr-defined]
    svc._vec._rows[f2.id].ts_last = now  # type: ignore[attr-defined]

    hits = await svc.recall(
        time_range=(now - 50.0, None),
        k=20, keyword_only=True, min_confidence=0.0,
    )
    ids = {h.fact.id for h in hits}
    assert f2.id in ids
    assert f1.id not in ids


@pytest.mark.asyncio
async def test_recall_time_range_until_only() -> None:
    """(None, end) = open-ended until-end."""
    import time as _t
    svc = _make_service()
    f1 = await svc.remember("old1", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    f2 = await svc.remember("new2", kind=FactKind.LESSON, scope=FactScope.PROJECT)
    now = _t.time()
    svc._vec._rows[f1.id].ts_last = now - 100.0  # type: ignore[attr-defined]
    svc._vec._rows[f2.id].ts_last = now  # type: ignore[attr-defined]

    hits = await svc.recall(
        time_range=(None, now - 50.0),
        k=20, keyword_only=True, min_confidence=0.0,
    )
    ids = {h.fact.id for h in hits}
    assert f1.id in ids
    assert f2.id not in ids


@pytest.mark.asyncio
async def test_recall_time_range_composes_with_layer_filter() -> None:
    """Phase 7 reflection_cycle shape: ``only_layer='working' +
    time_range=(N hours ago, now)`` replaces V1's short_term walk."""
    import time as _t
    svc = _make_service()
    working_recent = await svc.remember(
        "fresh working fact",
        kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="working",
    )
    working_old = await svc.remember(
        "stale working fact",
        kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="working",
    )
    long_term_recent = await svc.remember(
        "promoted recent fact",
        kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="long_term",
    )
    now = _t.time()
    svc._vec._rows[working_recent.id].ts_last = now  # type: ignore[attr-defined]
    svc._vec._rows[working_old.id].ts_last = now - 7200.0  # type: ignore[attr-defined]
    svc._vec._rows[long_term_recent.id].ts_last = now  # type: ignore[attr-defined]

    # "Last 1h of working layer" — reflection_cycle's new shape.
    hits = await svc.recall(
        only_layer="working",
        time_range=(now - 3600.0, None),
        k=20, keyword_only=True, min_confidence=0.0,
    )
    ids = {h.fact.id for h in hits}
    assert working_recent.id in ids
    assert working_old.id not in ids  # outside time window
    assert long_term_recent.id not in ids  # wrong layer


# ── Shim 7: LLMFactExtractor.extract_candidates + LLMCandidate ──


class _StubLLM:
    """Async LLM stub returning a fixed JSON-array response."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list[str] = []

    async def complete(self, messages, tools=None):  # noqa: ARG002
        prompt = messages[0].content if messages else ""
        self.calls.append(prompt)

        class _Resp:
            content = self._payload  # type: ignore[name-defined]

        # Bind self._payload at call-time so calls can vary if needed.
        _Resp.content = self._payload
        return _Resp()


@pytest.mark.asyncio
async def test_extract_candidates_returns_typed_dataclass() -> None:
    """Typed wrapper around extract() — V1 parity for hop_loop."""
    stub = _StubLLM(
        '[{"text": "用户喜欢简短回复",'
        ' "kind": "preference",'
        ' "scope": "user",'
        ' "confidence": 0.9}]'
    )
    ex = LLMFactExtractor(llm=stub)
    candidates = await ex.extract_candidates(
        user_message="我希望你尽量简短一些",
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert isinstance(cand, LLMCandidate)
    assert cand.text == "用户喜欢简短回复"
    assert cand.kind == "preference"
    assert cand.scope == "user"
    assert cand.confidence == 0.9


@pytest.mark.asyncio
async def test_extract_candidates_with_assistant_response() -> None:
    """assistant_response (Phase 7 shim) gets appended to the prompt
    so the extractor can see agent-confirmed facts."""
    stub = _StubLLM('[]')
    ex = LLMFactExtractor(llm=stub)
    await ex.extract_candidates(
        user_message="刚刚那个决定算最终的吗？",
        assistant_response="是的，已经定了用 LanceDB 作为后端。",
    )
    # Verify the prompt the LLM received includes the assistant text.
    assert len(stub.calls) == 1
    prompt = stub.calls[0]
    assert "已经定了用 LanceDB" in prompt
    assert "助手回复" in prompt
    assert "刚刚那个决定" in prompt


@pytest.mark.asyncio
async def test_extract_candidates_empty_on_no_user_message() -> None:
    ex = LLMFactExtractor(llm=_StubLLM("[]"))
    out = await ex.extract_candidates(user_message="")
    assert out == []


# ── Phase 7.B.1: MemoryService.sweep TTL + cap eviction ──────────


@pytest.mark.asyncio
async def test_sweep_ttl_prunes_old_working_facts() -> None:
    """TTL axis: working-layer facts older than ttl get deleted."""
    import time as _t
    svc = _make_service()
    f_old = await svc.remember(
        "old fact", kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="working",
    )
    f_fresh = await svc.remember(
        "fresh fact", kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="working",
    )
    now = _t.time()
    svc._vec._rows[f_old.id].ts_last = now - 7200.0  # type: ignore[attr-defined]
    svc._vec._rows[f_fresh.id].ts_last = now  # type: ignore[attr-defined]

    result = await svc.sweep(ttl={"working": 3600.0, "long_term": None})
    assert result["ttl_pruned"]["working"] == 1
    assert result["ttl_pruned"]["long_term"] == 0
    assert (await svc.get_fact(f_old.id)) is None
    assert (await svc.get_fact(f_fresh.id)) is not None


@pytest.mark.asyncio
async def test_sweep_max_items_evicts_oldest() -> None:
    """Cap axis: when count exceeds max_items, oldest get evicted.

    Uses semantically distinct texts so near-dup detection (cosine
    distance < 0.15) doesn't merge writes into the same fact —
    StubEmbedder's 4-dim vectors can be close enough that short
    similar strings collide.
    """
    import time as _t
    svc = _make_service()
    distinct_texts = [
        "用户最早决定使用 PostgreSQL 数据库",
        "agent 在 2025 年学会了 Python async",
        "项目 X 的 staging 环境在新加坡",
        "deadline 是十二月二十五号",
        "团队成员包括 Alice Bob Charlie",
    ]
    ids = []
    for i, text in enumerate(distinct_texts):
        f = await svc.remember(
            text, kind=FactKind.LESSON, scope=FactScope.PROJECT,
            layer="working",
        )
        ids.append(f.id)
        # Stamp distinct ts so oldest-first ordering is deterministic.
        svc._vec._rows[f.id].ts_last = _t.time() - (5 - i) * 10  # type: ignore[attr-defined]

    # Sanity: all 5 are distinct facts (no near-dup merging).
    assert len(set(ids)) == 5

    result = await svc.sweep(max_items={"working": 3})
    assert result["cap_evicted"]["working"] == 2
    # Oldest two (i=0, i=1) gone.
    assert (await svc.get_fact(ids[0])) is None
    assert (await svc.get_fact(ids[1])) is None
    # Newer three survive.
    for i in (2, 3, 4):
        assert (await svc.get_fact(ids[i])) is not None


@pytest.mark.asyncio
async def test_sweep_procedural_layer_exempt() -> None:
    """Procedural-layer facts never get touched by sweep."""
    import time as _t
    svc = _make_service()
    f = await svc.remember(
        "skill-fact", kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="procedural",
    )
    svc._vec._rows[f.id].ts_last = _t.time() - 999999.0  # type: ignore[attr-defined]

    result = await svc.sweep(
        ttl={"working": 1.0, "long_term": 1.0},
        max_items={"working": 0, "long_term": 0},
    )
    # Procedural not in returned summary.
    assert "procedural" not in result["ttl_pruned"]
    assert "procedural" not in result["cap_evicted"]
    # Fact still alive.
    assert (await svc.get_fact(f.id)) is not None


@pytest.mark.asyncio
async def test_sweep_protected_kinds_exempt() -> None:
    """identity + persona_manual facts survive both TTL + cap axes
    regardless of layer."""
    import time as _t
    svc = _make_service()
    f_id = await svc.remember(
        "用户叫 Alice",
        kind=FactKind.IDENTITY, scope=FactScope.USER,
        layer="working",
    )
    f_ord = await svc.remember(
        "regular fact",
        kind=FactKind.LESSON, scope=FactScope.PROJECT,
        layer="working",
    )
    svc._vec._rows[f_id.id].ts_last = _t.time() - 99999.0  # type: ignore[attr-defined]
    svc._vec._rows[f_ord.id].ts_last = _t.time() - 99999.0  # type: ignore[attr-defined]

    result = await svc.sweep(ttl={"working": 3600.0})
    # Only regular fact deleted.
    assert result["ttl_pruned"]["working"] == 1
    assert (await svc.get_fact(f_id.id)) is not None
    assert (await svc.get_fact(f_ord.id)) is None


@pytest.mark.asyncio
async def test_sweep_no_config_is_noop() -> None:
    """With no ttl/max_items/max_bytes specified, sweep does nothing."""
    svc = _make_service()
    f = await svc.remember(
        "stays", kind=FactKind.LESSON, scope=FactScope.PROJECT,
    )
    result = await svc.sweep()
    assert all(v == 0 for v in result["ttl_pruned"].values())
    assert all(v == 0 for v in result["cap_evicted"].values())
    assert (await svc.get_fact(f.id)) is not None
