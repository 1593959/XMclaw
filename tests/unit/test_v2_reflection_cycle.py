"""ReflectionCycle unit tests — R1 真持续认知 Loop (2026-05-10).

Coverage targets the three buckets independently:
  * reflect_recent   (5-min): drives an LLM, parses thoughts,
                              emits INNER_MONOLOGUE + REFLECTION_CYCLE_RAN
  * consolidate_memory (1-h): walks V2 MemoryService working layer,
                              promotes / dedupes / archives.
  * groom_goals       (1-d): prunes completed/stale; replans stuck.
And the dispatch layer (``run_due``) — only-due cycles fire.

Phase 7.A.3 (2026-05-23): consolidate tests rewritten for V2 — the
old V1 ``_FakeMemRich`` / ``_FakeMemBare`` stand-ins (which checked
duck-typed ``promote_durable_short_to_long`` etc.) are replaced by
``_FakeMemService`` exposing ``deduplicate`` + ``recall`` + ``remember``.
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
class _FakeFact:
    """Bare Fact stand-in. consolidate_memory only reads .text /
    .kind / .scope off it before calling memory_service.remember."""
    text: str
    kind: str = "lesson"
    scope: str = "project"


@dataclass
class _FakeHit:
    """Mimics RecallHit shape used by consolidate_memory."""
    fact: _FakeFact


@dataclass
class _FakeMemService:
    """V2 MemoryService stand-in for consolidate_memory tests.

    Records every call so tests can assert routing. ``recall``
    behaviour is driven by ``recent_facts`` / ``stale_facts``
    (selected by which time_range bound is set: ``time_range[0]``
    set = recent window; ``time_range[1]`` set = stale window).
    ``deduplicate`` returns ``dedupe_count``.
    """

    recent_facts: list[_FakeFact] = field(default_factory=list)
    stale_facts: list[_FakeFact] = field(default_factory=list)
    dedupe_count: int = 0
    calls: list[tuple[str, dict]] = field(default_factory=list)
    remembered: list[tuple[str, str]] = field(default_factory=list)

    async def deduplicate(self) -> int:
        self.calls.append(("deduplicate", {}))
        return self.dedupe_count

    async def recall(self, **kwargs: Any) -> list[_FakeHit]:
        self.calls.append(("recall", kwargs))
        tr = kwargs.get("time_range")
        # Same routing as consolidate_memory: (start, None) → recent;
        # (None, end) → stale.
        if tr and tr[0] is not None and tr[1] is None:
            return [_FakeHit(f) for f in self.recent_facts]
        if tr and tr[0] is None and tr[1] is not None:
            return [_FakeHit(f) for f in self.stale_facts]
        return []

    async def remember(self, **kwargs: Any) -> None:
        self.calls.append(("remember", kwargs))
        self.remembered.append((kwargs.get("text", ""), kwargs.get("layer", "")))


@dataclass
class _FakeMemServiceBare:
    """MemoryService stand-in missing every consolidate-required method
    — consolidate logs warnings + returns zeros without crashing."""

    async def deduplicate(self) -> int:
        raise AttributeError("simulated old-snapshot service")

    async def recall(self, **kwargs: Any) -> list[Any]:  # noqa: ARG002
        raise AttributeError("simulated")

    async def remember(self, **kwargs: Any) -> None:  # noqa: ARG002
        raise AttributeError("simulated")


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
async def test_consolidate_v2_native_path() -> None:
    """V2 MemoryService — dedupe + promote recent (Phase 4)."""
    mem = _FakeMemService(
        recent_facts=[_FakeFact("fresh A"), _FakeFact("fresh B")],
        stale_facts=[_FakeFact("old C")],
        dedupe_count=4,
    )
    bus = _CapturingBus()
    rc = ReflectionCycle(memory_service=mem, bus=bus, consolidate_batch=25)
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is True
    # Phase 4: merged = dedupe, promoted = recent (no LLM → no synthesis).
    assert out.summary["promoted"] == 2
    assert out.summary["merged"] == 4
    assert out.summary["synthesized"] == 0
    assert out.summary["superseded"] == 0
    assert out.summary["stale_marked"] == 0
    # Two remember() calls (2 promoted), all at layer=long_term.
    layers = [layer for _, layer in mem.remembered]
    assert layers == ["long_term", "long_term"]
    # Recall fired once (recent window only; no LLM → synthesize skipped).
    recall_calls = [c for c in mem.calls if c[0] == "recall"]
    assert len(recall_calls) == 1
    # Event published.
    types = [(e.type.value if hasattr(e.type, "value") else e.type)
             for e in bus.published]
    assert types == ["memory_consolidated"]


@pytest.mark.asyncio
async def test_consolidate_falls_back_when_v2_methods_missing() -> None:
    """A MemoryService snapshot that raises AttributeError on every
    method → consolidate logs + returns zeros, never crashes."""
    mem = _FakeMemServiceBare()
    bus = _CapturingBus()
    rc = ReflectionCycle(memory_service=mem, bus=bus)
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is True
    assert out.summary["promoted"] == 0
    assert out.summary["merged"] == 0
    assert out.summary["synthesized"] == 0
    assert out.summary["superseded"] == 0
    assert out.summary["stale_marked"] == 0


# ── Phase 4: LLM synthesis + stale detection ─────────────────────


@dataclass
class _FakeFactV4:
    """Fact stand-in with Phase 4 fields (id, bucket, layer, ts_last,
    confidence, superseded_by, invalid_at)."""
    text: str
    id: str = ""
    kind: str = "lesson"
    scope: str = "project"
    bucket: str = "misc"
    layer: str = "working"
    ts_last: float = 0.0
    confidence: float = 0.8
    superseded_by: str | None = None
    invalid_at: float | None = None


@dataclass
class _FakeHitV4:
    """RecallHit stand-in with distance."""
    fact: _FakeFactV4
    distance: float = 0.0


@dataclass
class _FakeMemServiceV4:
    """MemoryService stand-in for Phase 4 tests."""
    working_facts: list[_FakeFactV4] = field(default_factory=list)
    long_term_facts: list[_FakeFactV4] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)
    upserted: list[_FakeFactV4] = field(default_factory=list)

    async def deduplicate(self) -> int:
        self.calls.append(("deduplicate", {}))
        return 0

    async def recall(
        self, text: str | None = None, **kwargs: Any,
    ) -> list[_FakeHitV4]:
        self.calls.append(("recall", dict(kwargs, text=text)))
        only_layer = kwargs.get("only_layer")
        include_superseded = kwargs.get("include_superseded", True)
        if only_layer == "working":
            facts = self.working_facts
        elif only_layer == "long_term":
            facts = self.long_term_facts
        else:
            # Text-based recall (for stale detection).
            query = text or ""
            # Return neighbours from both layers.
            all_facts = self.working_facts + self.long_term_facts
            # Simple heuristic: return facts sharing the first 2 chars.
            matches = [
                f for f in all_facts
                if f.text[:2] == query[:2] and f.text != query
            ]
            return [_FakeHitV4(f, distance=0.1) for f in matches[:3]]
        out = []
        for f in facts:
            if not include_superseded and f.superseded_by:
                continue
            out.append(_FakeHitV4(f))
        return out

    async def remember(self, text: str, **kwargs: Any) -> _FakeFactV4:
        self.calls.append(("remember", dict(kwargs, text=text)))
        f = _FakeFactV4(
            text=text,
            id=f"fact-{len(self.calls)}",
            kind=kwargs.get("kind", "lesson"),
            scope=kwargs.get("scope", "project"),
            bucket=kwargs.get("bucket", "misc"),
            layer=kwargs.get("layer", "working"),
            confidence=kwargs.get("confidence", 0.8),
        )
        return f

    async def supersede(self, old_fact_id: str, new_fact_id: str) -> None:
        self.superseded.append((old_fact_id, new_fact_id))
        for f in self.working_facts + self.long_term_facts:
            if f.id == old_fact_id:
                f.superseded_by = new_fact_id

    @property
    def _vec(self):
        return self

    async def upsert(self, facts: list[Any]) -> None:
        self.upserted.extend(facts)

    async def get(self, fact_id: str) -> _FakeFactV4 | None:
        for f in self.working_facts + self.long_term_facts:
            if f.id == fact_id:
                return f
        return None


class _SynthesizeLLM:
    """LLM that returns a JSON array of synthesized statements."""
    def __init__(self, statements: list[str] | None = None) -> None:
        self.statements = statements or ["合成后的陈述"]
        self.calls = 0
        self.last_prompt = ""

    async def complete(self, messages: list, tools: Any = None) -> Any:
        self.calls += 1
        self.last_prompt = messages[-1].content if messages else ""
        import json
        return _FakeLLMResp(content=json.dumps({"statements": self.statements}))


@pytest.mark.asyncio
async def test_consolidate_synthesizes_fragments() -> None:
    """When LLM is wired, working facts are clustered by bucket and
    synthesized into long_term facts."""
    facts = [
        _FakeFactV4("用户喜欢 Python", id="w1", bucket="user_preference"),
        _FakeFactV4("用户偏好简洁的代码", id="w2", bucket="user_preference"),
        _FakeFactV4("用户讨厌冗余注释", id="w3", bucket="user_preference"),
        _FakeFactV4("项目使用 FastAPI", id="w4", bucket="project_fact"),
    ]
    mem = _FakeMemServiceV4(working_facts=facts)
    llm = _SynthesizeLLM(statements=["用户偏好简洁的 Python 代码，讨厌冗余注释"])
    rc = ReflectionCycle(
        llm=llm,
        memory_service=mem,
        bus=_CapturingBus(),
    )
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is True
    # user_preference bucket has 3 facts → 1 synthesis + 3 superseded.
    assert out.summary["synthesized"] == 1
    assert out.summary["superseded"] == 3
    # project_fact bucket has <3 facts → skipped.
    # LLM called once for the user_preference cluster.
    assert llm.calls == 1
    # The synthesized fact was written via remember() (no gateway).
    # + 1 promote call for the unsynthesized project_fact entry.
    remember_calls = [c for c in mem.calls if c[0] == "remember"]
    assert len(remember_calls) == 2
    layers = [c[1]["layer"] for c in remember_calls]
    assert all(l == "long_term" for l in layers)


@pytest.mark.asyncio
async def test_consolidate_stale_detection() -> None:
    """Long_term facts with close newer working neighbours get
    invalid_at stamped."""
    old = _FakeFactV4(
        "项目用 Flask", id="lt1", layer="long_term",
        ts_last=time.time() - 7200, bucket="project_fact",
    )
    new = _FakeFactV4(
        "项目已迁移到 FastAPI", id="w1", layer="working",
        ts_last=time.time(), bucket="project_fact",
    )
    mem = _FakeMemServiceV4(
        long_term_facts=[old],
        working_facts=[new],
    )
    rc = ReflectionCycle(
        llm=_SynthesizeLLM(),  # any LLM; stale_detect doesn't call it
        memory_service=mem,
        bus=_CapturingBus(),
    )
    out = await rc.consolidate_memory(tick=1)
    assert out.ran is True
    # stale_detect found lt1 as stale because w1 is close + newer.
    assert out.summary["stale_marked"] == 1
    assert old.invalid_at is not None
    assert old.confidence <= 0.3


@pytest.mark.asyncio
async def test_llm_synthesize_bucket_fallback_on_parse_error() -> None:
    """If LLM returns garbage JSON, fallback to the first fact's text."""
    facts = [
        _FakeFactV4("碎片 A", id="w1", bucket="misc"),
        _FakeFactV4("碎片 B", id="w2", bucket="misc"),
        _FakeFactV4("碎片 C", id="w3", bucket="misc"),
    ]
    llm = _FakeLLM(next_content="这不是 JSON")
    rc = ReflectionCycle(llm=llm, memory_service=None)
    result = await rc._llm_synthesize_bucket("misc", facts)
    assert result == ["碎片 A"]


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
    mem = _FakeMemService()
    bus = _CapturingBus()
    rc = ReflectionCycle(
        cognitive_state=state, memory_service=mem, bus=bus,
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
