"""Unit tests for Jarvis Phase 6.1: PerceptionBus + AttentionFilter."""
from __future__ import annotations

import time

import pytest

from xmclaw.cognition.attention_filter import AttentionFilter
from xmclaw.cognition.perception_bus import Percept, PerceptionBus


# -------------------------------------------------------------------- helpers


def make_percept(
    *,
    source: str = "ws",
    kind: str = "user_msg",
    content: str = "hello",
    suggested: float | None = None,
    timestamp: float | None = None,
    correlation_id: str | None = None,
    pid: str | None = None,
    extra_payload: dict | None = None,
) -> Percept:
    payload: dict = {"content": content}
    if extra_payload:
        payload.update(extra_payload)
    return Percept(
        id=pid or PerceptionBus.new_id(),
        source=source,  # type: ignore[arg-type]
        kind=kind,
        timestamp=timestamp if timestamp is not None else time.time(),
        payload=payload,
        suggested_salience=suggested,
        correlation_id=correlation_id,
    )


class FakeCognitiveState:
    """Minimal stand-in for CognitiveState used by AttentionFilter tests."""

    def __init__(
        self,
        score_map: dict[str, float] | None = None,
        default_score: float = 0.5,
    ) -> None:
        self.score_map = score_map or {}
        self.default_score = default_score
        self.attention_focus: list = []
        self.attention_capacity = 7
        self.salience_calls: list[dict] = []

    async def compute_salience(
        self,
        percept_id: str,
        content: str,
        *,
        urgency: float = 0.5,
        relevance: float | None = None,
        novelty: float = 0.5,
    ) -> float:
        self.salience_calls.append(
            {
                "percept_id": percept_id,
                "content": content,
                "urgency": urgency,
                "novelty": novelty,
                "relevance": relevance,
            }
        )
        return self.score_map.get(percept_id, self.default_score)

    def add_focus(self, focus) -> None:
        self.attention_focus.append(focus)
        if len(self.attention_focus) > self.attention_capacity:
            self.attention_focus.sort(key=lambda f: f.salience_score)
            self.attention_focus.pop(0)


# -------------------------------------------------------------------- bus core


@pytest.mark.asyncio
async def test_push_drain_round_trip_preserves_order() -> None:
    bus = PerceptionBus(max_buffer=8)
    a = make_percept(content="first")
    b = make_percept(content="second")
    c = make_percept(content="third")
    await bus.push(a)
    await bus.push(b)
    await bus.push(c)
    out = await bus.drain()
    assert [p.payload["content"] for p in out] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_drain_when_empty_returns_empty_list() -> None:
    bus = PerceptionBus()
    assert await bus.drain() == []


@pytest.mark.asyncio
async def test_drain_clears_buffer() -> None:
    bus = PerceptionBus()
    await bus.push(make_percept())
    await bus.drain()
    assert await bus.drain() == []
    assert bus.stats()["buffered"] == 0


@pytest.mark.asyncio
async def test_overflow_evicts_lowest_salience_not_fifo() -> None:
    bus = PerceptionBus(max_buffer=3)
    # Insert: low, high, mid, then a newcomer — `low` should be evicted,
    # not the FIFO-oldest (which is also `low` here, so we use a more
    # demonstrative ordering on the next test).
    high = make_percept(content="high", suggested=0.9)
    low = make_percept(content="low", suggested=0.1)
    mid = make_percept(content="mid", suggested=0.5)
    newcomer = make_percept(content="new", suggested=0.7)
    await bus.push(high)
    await bus.push(low)
    await bus.push(mid)
    await bus.push(newcomer)  # overflow
    out = await bus.drain()
    contents = {p.payload["content"] for p in out}
    assert contents == {"high", "mid", "new"}
    assert bus.stats()["total_dropped"] == 1


@pytest.mark.asyncio
async def test_overflow_eviction_prefers_lowest_when_oldest_is_high() -> None:
    """Make sure eviction is salience-driven, not FIFO."""
    bus = PerceptionBus(max_buffer=2)
    high_old = make_percept(content="old_high", suggested=0.95)
    low_new = make_percept(content="new_low", suggested=0.05)
    second_low = make_percept(content="second_low", suggested=0.10)
    await bus.push(high_old)
    await bus.push(low_new)
    await bus.push(second_low)  # overflow: low_new is now the worst (0.05)
    out = await bus.drain()
    contents = [p.payload["content"] for p in out]
    assert "old_high" in contents
    assert "second_low" in contents
    assert "new_low" not in contents


@pytest.mark.asyncio
async def test_overflow_treats_none_salience_as_lowest() -> None:
    bus = PerceptionBus(max_buffer=2)
    rated = make_percept(content="rated", suggested=0.5)
    unrated = make_percept(content="unrated", suggested=None)
    extra = make_percept(content="extra", suggested=0.9)
    await bus.push(rated)
    await bus.push(unrated)
    await bus.push(extra)
    out = await bus.drain()
    contents = {p.payload["content"] for p in out}
    assert contents == {"rated", "extra"}


@pytest.mark.asyncio
async def test_stats_reports_correct_counts() -> None:
    bus = PerceptionBus(max_buffer=2)
    await bus.push(make_percept(suggested=0.1))
    await bus.push(make_percept(suggested=0.2))
    await bus.push(make_percept(suggested=0.9))  # forces a drop
    stats_before_drain = bus.stats()
    assert stats_before_drain["total_pushed"] == 3
    assert stats_before_drain["total_dropped"] == 1
    assert stats_before_drain["buffered"] == 2
    assert stats_before_drain["max_buffer"] == 2
    await bus.drain()
    stats_after_drain = bus.stats()
    assert stats_after_drain["total_drained"] == 2
    assert stats_after_drain["buffered"] == 0


def test_invalid_max_buffer_rejected() -> None:
    with pytest.raises(ValueError):
        PerceptionBus(max_buffer=0)


# -------------------------------------------------------------------- subs


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_lifecycle() -> None:
    bus = PerceptionBus()
    seen: list[Percept] = []

    async def collector(p: Percept) -> None:
        seen.append(p)

    sub_id = bus.subscribe(collector)
    assert isinstance(sub_id, str) and len(sub_id) > 0
    await bus.push(make_percept(content="one"))
    bus.unsubscribe(sub_id)
    await bus.push(make_percept(content="two"))
    assert [p.payload["content"] for p in seen] == ["one"]


@pytest.mark.asyncio
async def test_unsubscribe_unknown_id_is_noop() -> None:
    bus = PerceptionBus()
    bus.unsubscribe("not-a-real-id")  # must not raise


@pytest.mark.asyncio
async def test_subscriber_sees_every_push() -> None:
    bus = PerceptionBus()
    seen: list[str] = []

    async def collector(p: Percept) -> None:
        seen.append(p.payload["content"])

    bus.subscribe(collector)
    for content in ("a", "b", "c", "d"):
        await bus.push(make_percept(content=content))
    assert seen == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_subscriber_exception_is_swallowed() -> None:
    bus = PerceptionBus()

    async def angry(_p: Percept) -> None:
        raise RuntimeError("boom")

    bus.subscribe(angry)
    # Push must not raise even though the subscriber blew up.
    await bus.push(make_percept(content="ok"))
    out = await bus.drain()
    assert len(out) == 1


@pytest.mark.asyncio
async def test_one_bad_subscriber_does_not_block_others() -> None:
    bus = PerceptionBus()
    good_seen: list[Percept] = []

    async def angry(_p: Percept) -> None:
        raise RuntimeError("boom")

    async def good(p: Percept) -> None:
        good_seen.append(p)

    bus.subscribe(angry)
    bus.subscribe(good)
    await bus.push(make_percept(content="hello"))
    assert len(good_seen) == 1


@pytest.mark.asyncio
async def test_stats_tracks_subscriber_count() -> None:
    bus = PerceptionBus()
    assert bus.stats()["subscribers"] == 0

    async def noop(_p: Percept) -> None:
        return None

    sid = bus.subscribe(noop)
    assert bus.stats()["subscribers"] == 1
    bus.unsubscribe(sid)
    assert bus.stats()["subscribers"] == 0


# -------------------------------------------------------------- attention filter


@pytest.mark.asyncio
async def test_attention_filter_empty_bus_returns_empty() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    assert await af.tick() == []
    assert cs.salience_calls == []


@pytest.mark.asyncio
async def test_attention_filter_invokes_compute_salience() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState(default_score=0.5)
    af = AttentionFilter(cs, bus)
    p = make_percept(content="hi", pid="p1")
    await bus.push(p)
    await af.tick()
    assert len(cs.salience_calls) == 1
    call = cs.salience_calls[0]
    assert call["percept_id"] == "p1"
    assert call["content"] == "hi"
    assert "urgency" in call
    assert "novelty" in call


@pytest.mark.asyncio
async def test_attention_filter_returns_only_above_threshold() -> None:
    bus = PerceptionBus()
    score_map = {"high": 0.9, "low": 0.2, "mid": 0.65}
    cs = FakeCognitiveState(score_map=score_map)
    af = AttentionFilter(cs, bus, action_threshold=0.6)
    await bus.push(make_percept(pid="high", content="h"))
    await bus.push(make_percept(pid="low", content="l"))
    await bus.push(make_percept(pid="mid", content="m"))
    out = await af.tick()
    out_ids = {p.id for p in out}
    assert out_ids == {"high", "mid"}


@pytest.mark.asyncio
async def test_attention_filter_updates_focus_for_all_percepts() -> None:
    bus = PerceptionBus()
    score_map = {"high": 0.9, "low": 0.2}
    cs = FakeCognitiveState(score_map=score_map)
    af = AttentionFilter(cs, bus, action_threshold=0.6, top_k_focus=7)
    await bus.push(make_percept(pid="high", content="hello world"))
    await bus.push(make_percept(pid="low", content="quiet"))
    await af.tick()
    # Both percepts get added to the focus pool — threshold gates the
    # ACTIONABLE return, not the focus update.
    focus_ids = {f.percept_id for f in cs.attention_focus}
    assert {"high", "low"}.issubset(focus_ids)


@pytest.mark.asyncio
async def test_attention_filter_top_k_focus_cap_enforced() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState(default_score=0.5)
    af = AttentionFilter(cs, bus, top_k_focus=3)
    # Filter pushed top_k_focus down onto the host cognitive state.
    assert cs.attention_capacity == 3
    for i in range(5):
        await bus.push(make_percept(pid=f"p{i}", content=f"c{i}"))
    await af.tick()
    assert len(cs.attention_focus) <= 3


@pytest.mark.asyncio
async def test_attention_filter_top_k_keeps_highest_scoring() -> None:
    bus = PerceptionBus()
    score_map = {f"p{i}": 0.1 * i for i in range(5)}
    cs = FakeCognitiveState(score_map=score_map)
    af = AttentionFilter(cs, bus, top_k_focus=2, action_threshold=0.0)
    for i in range(5):
        await bus.push(make_percept(pid=f"p{i}", content=f"c{i}"))
    await af.tick()
    kept_ids = {f.percept_id for f in cs.attention_focus}
    assert kept_ids == {"p3", "p4"}


@pytest.mark.asyncio
async def test_infer_urgency_lookup_table() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    user_msg = make_percept(source="ws", kind="user_msg")
    process_oom = make_percept(source="process", kind="process_oom")
    time_tick = make_percept(source="time", kind="time_tick")
    # Relative ordering matters more than exact values.
    assert af._infer_urgency(process_oom) > af._infer_urgency(user_msg)
    assert af._infer_urgency(user_msg) > af._infer_urgency(time_tick)
    # And specific anchors.
    assert af._infer_urgency(user_msg) >= 0.7
    assert af._infer_urgency(time_tick) <= 0.2


@pytest.mark.asyncio
async def test_infer_urgency_explicit_override() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    p = make_percept(
        source="time",
        kind="time_tick",
        extra_payload={"urgency": 0.9},
    )
    assert af._infer_urgency(p) == 0.9


@pytest.mark.asyncio
async def test_infer_urgency_falls_back_to_source_then_default() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    # Unknown kind, known source.
    p1 = make_percept(source="ws", kind="never_seen_kind")
    assert af._infer_urgency(p1) == 0.7
    # Unknown source AND kind. We force this with a Percept built
    # outside the helper to bypass the Literal type — at runtime
    # frozen dataclass still accepts any string.
    p2 = Percept(
        id="x",
        source="other",  # type: ignore[arg-type]
        kind="who_knows",
        timestamp=time.time(),
        payload={},
    )
    assert af._infer_urgency(p2) == 0.5


@pytest.mark.asyncio
async def test_novelty_first_time_is_one() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    p = make_percept(content="brand new")
    assert await af._novelty(p) == 1.0


@pytest.mark.asyncio
async def test_novelty_repeat_is_lower() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    t = 1_000_000.0
    p1 = make_percept(content="dupe", timestamp=t)
    p2 = make_percept(content="dupe", timestamp=t + 1.0)  # 1s later
    first = await af._novelty(p1)
    second = await af._novelty(p2)
    assert first == 1.0
    assert second < first
    # After a long gap the same content should look more novel again.
    p3 = make_percept(content="dupe", timestamp=t + 120.0)
    third = await af._novelty(p3)
    assert third > second


@pytest.mark.asyncio
async def test_novelty_lru_cap_evicts_oldest() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    af = AttentionFilter(cs, bus)
    # Fill the LRU with 128 unique entries.
    cap = AttentionFilter._NOVELTY_CACHE_SIZE
    for i in range(cap):
        await af._novelty(make_percept(content=f"unique-{i}"))
    # The very first one should still be remembered (cache exactly full).
    repeat = await af._novelty(make_percept(content="unique-0"))
    assert repeat < 1.0
    # Now overflow the LRU by one. The earliest entry ("unique-0") must
    # be evicted, so a fresh lookup of it looks novel again.
    # (We just touched unique-0 above, moving it to MRU, so push enough
    # new uniques to bury it again.)
    for j in range(cap):
        await af._novelty(make_percept(content=f"overflow-{j}"))
    # unique-1 was the oldest still-untouched original entry; it should
    # have fallen out.
    novelty_after_eviction = await af._novelty(
        make_percept(content="unique-1")
    )
    assert novelty_after_eviction == 1.0


@pytest.mark.asyncio
async def test_attention_filter_invalid_threshold_rejected() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState()
    with pytest.raises(ValueError):
        AttentionFilter(cs, bus, action_threshold=1.5)
    with pytest.raises(ValueError):
        AttentionFilter(cs, bus, action_threshold=-0.1)
    with pytest.raises(ValueError):
        AttentionFilter(cs, bus, top_k_focus=0)


@pytest.mark.asyncio
async def test_attention_filter_handles_percept_without_content_key() -> None:
    bus = PerceptionBus()
    cs = FakeCognitiveState(default_score=0.7)
    af = AttentionFilter(cs, bus, action_threshold=0.5)
    # No 'content' in payload — filter should still extract something
    # usable rather than crashing.
    weird = Percept(
        id="w1",
        source="file",
        kind="file_modified",
        timestamp=time.time(),
        payload={"path": "/tmp/foo.txt"},
    )
    await bus.push(weird)
    out = await af.tick()
    assert len(out) == 1
    # compute_salience was called with SOMETHING for content.
    assert cs.salience_calls[0]["content"]
