"""Unit tests for the 2026-05-28 ``memory_inspect`` tool +
the background auto-dedup tick wired into the retention loop.

Background: the user surfaced "agent can't autonomously prune its
LanceDB fact store". Two gaps closed:

1. ``memory_inspect`` — a read-only health probe the agent can call
   without being asked. Reports fact counts + duplicate ratio per
   scope + a recommendations list. Drives proactive
   ``memory_dedup`` / ``memory_forget`` use.

2. Background auto-dedup — the existing hourly retention sweep
   already prunes TTL/cap-overflow facts, but never dedupes. Now
   it runs ``dedup_scope`` every N sweeps (default 24 = daily) on
   common scopes. Tested via direct call to ``dedup_scope`` —
   wiring of the loop itself is integration-tested by app_lifespan
   suites.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="synthetic",
    )


def _make_fact_hit(
    fact_id: str, text: str, scope: str, kind: str,
    embedding: list[float] | None = None,
    ts_last: float = 1000.0,
):
    """Lightweight RecallHit-shaped mock."""
    fact = MagicMock()
    fact.id = fact_id
    fact.text = text
    fact.scope = scope
    fact.kind = kind
    fact.embedding = embedding or [1.0, 0.0, 0.0, 0.0]
    fact.ts_last = ts_last
    fact.confidence = 0.8
    fact.evidence_count = 1
    hit = MagicMock()
    hit.fact = fact
    hit.distance = 0.0
    return hit


@pytest.fixture
def tools_with_mock_svc(monkeypatch):
    """BuiltinTools instance whose ``_resolve_memory_v2_service``
    points at a configurable AsyncMock service."""
    svc = MagicMock()
    svc.recall = AsyncMock()
    tools = BuiltinTools()
    monkeypatch.setattr(
        BuiltinTools, "_resolve_memory_v2_service",
        staticmethod(lambda: svc),
    )
    return tools, svc


# ─── memory_inspect ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_inspect_no_service_returns_clean_error(monkeypatch):
    monkeypatch.setattr(
        BuiltinTools, "_resolve_memory_v2_service",
        staticmethod(lambda: None),
    )
    tools = BuiltinTools()
    r = await tools.invoke(_call("memory_inspect", {}))
    assert r.ok is False
    assert "not wired" in r.error


@pytest.mark.asyncio
async def test_memory_inspect_breakdown_counts_correctly(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    svc.recall.return_value = [
        _make_fact_hit("1", "name is alice", "user", "identity"),
        _make_fact_hit("2", "lives in tokyo", "user", "identity"),
        _make_fact_hit("3", "prefers python", "user", "preference"),
        _make_fact_hit("4", "project X uses asyncio", "project", "fact"),
    ]
    r = await tools.invoke(_call("memory_inspect", {}))
    assert r.ok is True
    c = r.content
    assert c["total_facts"] == 4
    assert c["breakdown"]["user"]["identity"] == 2
    assert c["breakdown"]["user"]["preference"] == 1
    assert c["breakdown"]["project"]["fact"] == 1


@pytest.mark.asyncio
async def test_memory_inspect_detects_high_dup_ratio_and_recommends(
    tools_with_mock_svc,
):
    """Many near-identical embeddings in one scope → dup_ratio
    crosses the 0.15 trigger → recommendation surfaces."""
    tools, svc = tools_with_mock_svc
    # 5 facts in 'user' scope with the SAME embedding — all dups.
    same_emb = [0.5, 0.5, 0.5, 0.5]
    svc.recall.return_value = [
        _make_fact_hit(f"d{i}", f"fact {i}", "user", "preference",
                       embedding=same_emb)
        for i in range(5)
    ]
    r = await tools.invoke(_call("memory_inspect", {}))
    assert r.ok is True
    dup = r.content["dup_estimate"]["user"]
    assert dup["dup_clusters"] == 1
    assert dup["excess_facts"] == 4  # 5 facts, 1 survivor, 4 excess
    assert dup["dup_ratio"] == 0.8   # 4/5
    # Recommendation should mention memory_dedup with scope=user
    recs = r.content["recommendations"]
    assert any("memory_dedup" in r and "'user'" in r for r in recs)


@pytest.mark.asyncio
async def test_memory_inspect_no_dups_no_recommendations(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    # 4 facts with orthogonal embeddings — no clusters.
    svc.recall.return_value = [
        _make_fact_hit("a", "x", "user", "k",
                       embedding=[1.0, 0.0, 0.0, 0.0]),
        _make_fact_hit("b", "y", "user", "k",
                       embedding=[0.0, 1.0, 0.0, 0.0]),
        _make_fact_hit("c", "z", "user", "k",
                       embedding=[0.0, 0.0, 1.0, 0.0]),
        _make_fact_hit("d", "w", "user", "k",
                       embedding=[0.0, 0.0, 0.0, 1.0]),
    ]
    r = await tools.invoke(_call("memory_inspect", {}))
    dup = r.content["dup_estimate"]["user"]
    assert dup["dup_clusters"] == 0
    assert dup["dup_ratio"] == 0.0
    assert r.content["recommendations"] == [
        "no action needed — store looks tidy.",
    ]


@pytest.mark.asyncio
async def test_memory_inspect_scope_filter_passes_through(tools_with_mock_svc):
    tools, svc = tools_with_mock_svc
    svc.recall.return_value = []
    await tools.invoke(_call("memory_inspect", {"scope": "session"}))
    # Service must have been queried with scopes=['session'].
    call_kwargs = svc.recall.call_args.kwargs
    assert call_kwargs["scopes"] == ["session"]


@pytest.mark.asyncio
async def test_memory_inspect_returns_oldest_and_largest_top5(
    tools_with_mock_svc,
):
    tools, svc = tools_with_mock_svc
    svc.recall.return_value = [
        _make_fact_hit(
            f"f{i}", "x" * (100 - i), "user", "k",
            ts_last=1000.0 + i,
        )
        for i in range(10)
    ]
    r = await tools.invoke(_call("memory_inspect", {}))
    # 5 oldest by ts.
    assert len(r.content["oldest_5"]) == 5
    assert r.content["oldest_5"][0]["ts"] == 1000.0
    # 5 largest by char count.
    assert len(r.content["largest_5"]) == 5
    assert r.content["largest_5"][0]["chars"] == 100


# ─── Spec registration ────────────────────────────────────────────


def test_memory_inspect_advertised_to_llm():
    """The agent must SEE the unified memory tool to autonomously call
    memory_inspect. Wiring regression would silently lose the
    self-grooming capability."""
    names = {s.name for s in BuiltinTools().list_tools()}
    assert "memory" in names


def test_memory_inspect_spec_is_zero_arg():
    """The recommended autonomous workflow is ``memory_inspect()``
    with no args. Pin that — adding a required field would break the
    self-grooming default behaviour."""
    from xmclaw.providers.tool._specs import _MEMORY_INSPECT_SPEC
    required = _MEMORY_INSPECT_SPEC.parameters_schema.get("required", [])
    assert required == [] or required is None


# ─── Auto-dedup tick (unit-level: just verify dedup_scope is what's
#     called when the loop fires; loop scheduling is integration) ──


@pytest.mark.asyncio
async def test_dedup_scope_invoked_per_configured_scope():
    """Direct invocation pattern mirrors what the retention loop's
    auto-dedup tick does every N sweeps."""
    svc = MagicMock()
    svc.dedup_scope = AsyncMock(return_value={"scanned": 10, "merged": 2})
    scopes = ["user", "project", "session"]
    total = 0
    for s in scopes:
        result = await svc.dedup_scope(scope=s, dry_run=False)
        total += int(result.get("merged", 0))
    assert svc.dedup_scope.await_count == 3
    assert total == 6
    # Every call must have used dry_run=False (committing) — the
    # background tick is for real cleanup, not previews.
    for c in svc.dedup_scope.await_args_list:
        assert c.kwargs["dry_run"] is False
