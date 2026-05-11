"""ReflectionCycle unit tests — R1 真持续认知 Loop (2026-05-10).

Coverage targets the three buckets independently:
  * reflect_recent   (5-min): drives an LLM, parses thoughts,
                              emits INNER_MONOLOGUE + REFLECTION_CYCLE_RAN
  * consolidate_memory (1-h): walks UnifiedMemory's optional rich-API
                              hooks; counts dry-run when missing.
  * groom_goals       (1-d): prunes completed/stale; replans stuck.
And the dispatch layer (``run_due``) — only-due cycles fire.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.cognition.reflection_cycle import (
    ReflectionCycle,
)


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _FakeLLMResp:
    content: str


@dataclass
class _FakeLLM:
    next_content: str = "[]"
    last_prompt: str = ""
    calls: int = 0

    async def complete(self, messages: list, tools: Any = None) -> Any:  # noqa: ARG002
        self.calls += 1
        self.last_prompt = messages[-1].content if messages else ""
        return _FakeLLMResp(content=self.next_content)


@dataclass
class _CapturingBus:
    published: list[Any] = field(default_factory=list)

    async def publish(self, event: Any) -> None:
        self.published.append(event)


@dataclass
class _FakeMemRich:
    """UnifiedMemorySystem stand-in exposing the OPTIONAL
    promote/merge/archive hooks. Counts calls."""
    promotes: int = 0
    merges: int = 0
    archives: int = 0
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def promote_durable_short_to_long(self, *, batch: int) -> int:
        self.calls.append(("promote", batch))
        return self.promotes

    async def merge_near_duplicates(self, *, batch: int) -> int:
        self.calls.append(("merge", batch))
        return self.merges

    async def archive_stale_short(self, *, batch: int) -> int:
        self.calls.append(("archive", batch))
        return self.archives


@dataclass
class _FakeMemBare:
    """UnifiedMemorySystem without the rich hooks (current shipping
    version) — consolidate falls through to dry counts."""
    pass


@dataclass
class _Goal:
    id: str
    description: str
    status: str
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class _FakeState:
    current_goals: list[_Goal] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────


def _make_event_bag(types_payload: list[tuple[str, dict]]) -> list[Any]:
    """Mimics BehavioralEvent shape (.type / .payload) for the
    recent_events_fn return."""
    @dataclass
    class _E:
        type: str
        payload: dict
    return [_E(t, p) for t, p in types_payload]


# ── Scope 1: reflect_recent ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reflect_recent_skips_when_no_llm() -> None:
    rc = ReflectionCycle(llm=None, recent_events_fn=lambda n: _async([]))
    out = await rc.reflect_recent(tick=10)
    assert out.ran is False


@pytest.mark.asyncio
async def test_reflect_recent_skips_when_no_recent_events_fn() -> None:
    rc = ReflectionCycle(llm=_FakeLLM())
    out = await rc.reflect_recent(tick=10)
    assert out.ran is False


@pytest.mark.asyncio
async def test_reflect_recent_skips_on_empty_journal() -> None:
    """When the journal returns 0 events, the cycle exits without
    calling the LLM (saves an API call)."""
    llm = _FakeLLM()
    bus = _CapturingBus()

    async def _empty(n: int) -> list:
        return []

    rc = ReflectionCycle(llm=llm, bus=bus, recent_events_fn=_empty)
    out = await rc.reflect_recent(tick=10)
    assert out.ran is False
    assert llm.calls == 0
    assert bus.published == []


@pytest.mark.asyncio
async def test_reflect_recent_emits_inner_thoughts_and_cycle_summary() -> None:
    llm = _FakeLLM(next_content=(
        '[{"kind": "reflection", "text": "用户问 X 三次，我都答得不够直接。",'
        ' "trigger": "三次相同问题"},'
        ' {"kind": "plan", "text": "下次直接给代码示例。",'
        ' "trigger": "用户偏好"}]'
    ))
    bus = _CapturingBus()
    events = _make_event_bag([
        ("user_message", {"content": "X 怎么做"}),
        ("llm_response", {"content": "..."}),
        ("user_message", {"content": "再问一次 X"}),
    ])

    async def _recent(n: int) -> list:
        return events[:n]

    rc = ReflectionCycle(
        llm=llm, bus=bus,
        recent_events_fn=_recent,
        reflect_lookback_turns=10,
    )
    out = await rc.reflect_recent(tick=42)
    assert out.ran is True
    assert llm.calls == 1
    # 2 INNER_MONOLOGUE + 1 REFLECTION_CYCLE_RAN.
    types = [str(e.type.value if hasattr(e.type, "value") else e.type)
             for e in bus.published]
    assert types.count("inner_monologue") == 2
    assert types.count("reflection_cycle_ran") == 1
    # The reflection summary carries the patterns text.
    summary_payload = next(
        (e.payload for e in bus.published
         if (e.type.value if hasattr(e.type, "value") else e.type)
         == "reflection_cycle_ran"),
        {},
    )
    assert summary_payload["lookback_n"] == 3
    assert len(summary_payload["patterns_found"]) == 2


@pytest.mark.asyncio
async def test_reflect_recent_handles_llm_returning_empty_list() -> None:
    """LLM agreed nothing's worth reflecting on — no thoughts emitted,
    no REFLECTION_CYCLE_RAN suppressed (we still emit a summary so
    the UI shows the cycle ran)."""
    llm = _FakeLLM(next_content="[]")
    bus = _CapturingBus()
    events = _make_event_bag([("user_message", {"content": "天气"})])

    async def _recent(n: int) -> list:
        return events

    rc = ReflectionCycle(llm=llm, bus=bus, recent_events_fn=_recent)
    out = await rc.reflect_recent(tick=5)
    assert out.ran is True
    types = [(e.type.value if hasattr(e.type, "value") else e.type)
             for e in bus.published]
    assert "inner_monologue" not in types
    assert types.count("reflection_cycle_ran") == 1


@pytest.mark.asyncio
async def test_reflect_recent_strips_markdown_fence_from_llm() -> None:
    llm = _FakeLLM(next_content=(
        '```json\n[{"kind":"observation","text":"hi","trigger":"x"}]\n```'
    ))
    bus = _CapturingBus()

    async def _recent(n: int) -> list:
        return _make_event_bag([("ev", {})])

    rc = ReflectionCycle(llm=llm, bus=bus, recent_events_fn=_recent)
    out = await rc.reflect_recent(tick=1)
    assert out.ran
    assert any(
        (e.type.value if hasattr(e.type, "value") else e.type)
        == "inner_monologue" for e in bus.published
    )


@pytest.mark.asyncio
async def test_reflect_recent_clamps_invalid_kind_to_observation() -> None:
    llm = _FakeLLM(next_content=(
        '[{"kind":"galaxy","text":"weird","trigger":"x"}]'
    ))
    bus = _CapturingBus()

    async def _recent(n: int) -> list:
        return _make_event_bag([("ev", {})])

    rc = ReflectionCycle(llm=llm, bus=bus, recent_events_fn=_recent)
    out = await rc.reflect_recent(tick=1)
    assert out.ran
    monologues = [
        e for e in bus.published
        if (e.type.value if hasattr(e.type, "value") else e.type)
        == "inner_monologue"
    ]
    assert len(monologues) == 1
    assert monologues[0].payload["kind"] == "observation"


@pytest.mark.asyncio
async def test_reflect_recent_handles_llm_exception() -> None:
    """LLM raised → cycle returns (no thoughts), but doesn't crash."""

    class _BoomLLM:
        async def complete(self, *_a, **_kw):
            raise RuntimeError("net dead")

    bus = _CapturingBus()

    async def _recent(n: int) -> list:
        return _make_event_bag([("ev", {})])

    rc = ReflectionCycle(llm=_BoomLLM(), bus=bus, recent_events_fn=_recent)
    out = await rc.reflect_recent(tick=1)
    assert out.ran is True  # cycle ran (just produced no thoughts)


# ── Scope 2: consolidate_memory ──────────────────────────────────


@pytest.mark.asyncio
async def test_consolidate_skips_when_no_memory() -> None:
    rc = ReflectionCycle()
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is False


@pytest.mark.asyncio
async def test_consolidate_calls_rich_api_and_counts() -> None:
    mem = _FakeMemRich(promotes=3, merges=1, archives=2)
    bus = _CapturingBus()
    rc = ReflectionCycle(unified_memory=mem, bus=bus, consolidate_batch=25)
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is True
    assert out.summary["promoted"] == 3
    assert out.summary["merged"] == 1
    assert out.summary["archived"] == 2
    # All three rich hooks were tried with the configured batch size.
    assert mem.calls == [
        ("promote", 25), ("merge", 25), ("archive", 25),
    ]
    # Event published.
    types = [(e.type.value if hasattr(e.type, "value") else e.type)
             for e in bus.published]
    assert types == ["memory_consolidated"]


@pytest.mark.asyncio
async def test_consolidate_falls_back_when_no_rich_hooks() -> None:
    """UnifiedMemorySystem without optional methods → counts stay 0
    but cycle still runs + emits event."""
    mem = _FakeMemBare()
    bus = _CapturingBus()
    rc = ReflectionCycle(unified_memory=mem, bus=bus)
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is True
    assert out.summary == {
        "promoted": 0, "merged": 0, "archived": 0,
        "elapsed_ms": out.summary["elapsed_ms"],
    }


# ── Scope 3: groom_goals ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_groom_skips_when_no_state() -> None:
    rc = ReflectionCycle()
    out = await rc.groom_goals(tick=1)
    assert out.ran is False


@pytest.mark.asyncio
async def test_groom_skips_when_no_goals() -> None:
    state = _FakeState(current_goals=[])
    rc = ReflectionCycle(cognitive_state=state)
    out = await rc.groom_goals(tick=1)
    assert out.ran is False


@pytest.mark.asyncio
async def test_groom_archives_completed_drops_stale_replans_stuck() -> None:
    now = time.time()
    state = _FakeState(current_goals=[
        _Goal("g1", "done thing", "completed", created_at=now - 100,
              updated_at=now - 100),
        _Goal("g2", "fresh", "active", created_at=now - 10,
              updated_at=now - 10),
        _Goal("g3", "stuck a long time", "blocked",
              created_at=now - 200000, updated_at=now - 200000),
        _Goal("g4", "abandoned", "active",
              created_at=now - 30 * 86400, updated_at=now - 30 * 86400),
    ])
    bus = _CapturingBus()
    rc = ReflectionCycle(
        cognitive_state=state, bus=bus,
        groom_stale_days=7,
        groom_blocked_hours=24,
    )
    out = await rc.groom_goals(tick=1)
    assert out.ran
    s = out.summary
    assert s["before"] == 4
    assert s["completed_archived"] == 1
    assert s["stale_dropped"] == 1
    assert s["stuck_replanned"] == 1
    assert s["after"] == 2  # fresh + replanned-stuck
    # Stuck goal should have been re-marked.
    statuses = {g.id: g.status for g in state.current_goals}
    assert statuses["g2"] == "active"
    assert statuses["g3"] == "needs_replan"
    # Event published.
    types = [(e.type.value if hasattr(e.type, "value") else e.type)
             for e in bus.published]
    assert types == ["goals_groomed"]


# ── Dispatch: run_due ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_due_fires_each_bucket_only_when_due() -> None:
    """Single-bucket due → only that bucket runs."""
    state = _FakeState(current_goals=[
        _Goal("g1", "x", "active", created_at=time.time(),
              updated_at=time.time()),
    ])
    bus = _CapturingBus()
    rc = ReflectionCycle(
        cognitive_state=state, bus=bus,
        # tiny periods so we can drive ticks deterministically
        reflect_every_ticks=10**9,   # never due
        consolidate_every_ticks=10**9,  # never due
        groom_every_ticks=5,
    )
    # First tick — groom is due (last_ran=-1 sentinel).
    results = await rc.run_due(tick=1)
    assert [r.scope for r in results] == ["groom"]
    # Tick 2 — groom NOT due yet (5-tick period).
    results = await rc.run_due(tick=2)
    assert results == []
    # Tick 6 — groom due again.
    results = await rc.run_due(tick=6)
    assert [r.scope for r in results] == ["groom"]


@pytest.mark.asyncio
async def test_run_due_fires_multiple_buckets_in_one_tick() -> None:
    state = _FakeState(current_goals=[
        _Goal("g", "x", "active", created_at=time.time(),
              updated_at=time.time()),
    ])
    mem = _FakeMemRich()
    bus = _CapturingBus()
    rc = ReflectionCycle(
        cognitive_state=state, unified_memory=mem, bus=bus,
        reflect_every_ticks=10**9,  # disabled (no llm anyway)
        consolidate_every_ticks=1,
        groom_every_ticks=1,
    )
    results = await rc.run_due(tick=1)
    scopes = {r.scope for r in results}
    assert "consolidate" in scopes
    assert "groom" in scopes


# ── helpers ──────────────────────────────────────────────────────


async def _async(value):
    return value
