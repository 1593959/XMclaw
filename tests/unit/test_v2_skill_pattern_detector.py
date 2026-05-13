"""Sprint 2 Wave 19 — SkillPatternDetector unit tests."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from xmclaw.cognition.skill_pattern_detector import (
    Pattern,
    _is_subsequence_of_any,
    analyze_patterns,
    format_pattern_proposal,
)


def _ev(session: str, name: str):
    return SimpleNamespace(
        type="tool_call_emitted",
        payload={"name": name},
        session_id=session,
        ts=time.time(),
    )


def _mock_bus(events: list) -> MagicMock:
    bus = MagicMock()
    bus.query.return_value = events
    return bus


# ── empty / degenerate inputs ────────────────────────────────────


def test_no_events_returns_empty():
    bus = _mock_bus([])
    assert analyze_patterns(bus=bus) == []


def test_bus_without_query_returns_empty():
    # Plain object — no .query attribute → graceful empty.
    assert analyze_patterns(bus=SimpleNamespace()) == []


def test_bus_query_raising_returns_empty():
    bus = MagicMock()
    bus.query.side_effect = RuntimeError("db locked")
    assert analyze_patterns(bus=bus) == []


# ── repeated patterns ────────────────────────────────────────────


def test_pattern_in_three_sessions_is_detected():
    """Same A→B→C sequence in 3 different sessions → must surface."""
    events = []
    for sid in ("s1", "s2", "s3"):
        events.append(_ev(sid, "screen_capture"))
        events.append(_ev(sid, "image_read"))
        events.append(_ev(sid, "gui_send_chat"))
    patterns = analyze_patterns(bus=_mock_bus(events))
    seqs = [p.tool_sequence for p in patterns]
    # The longest matching ngram (length-3) should win the dedup.
    assert ("screen_capture", "image_read", "gui_send_chat") in seqs


def test_pattern_in_fewer_than_min_sessions_filtered():
    """Pattern in only 2 sessions, default min=3, → filtered out."""
    events = [
        _ev("s1", "a"), _ev("s1", "b"),
        _ev("s2", "a"), _ev("s2", "b"),
    ]
    patterns = analyze_patterns(bus=_mock_bus(events))
    assert patterns == []


def test_single_tool_repetition_not_a_pattern():
    """A user clicking the same button 100 times in one session is
    NOT a workflow worth skilling."""
    events = [
        _ev("s1", "screen_capture"),
        _ev("s1", "screen_capture"),
        _ev("s1", "screen_capture"),
        _ev("s2", "screen_capture"),
        _ev("s2", "screen_capture"),
        _ev("s3", "screen_capture"),
        _ev("s3", "screen_capture"),
    ]
    patterns = analyze_patterns(bus=_mock_bus(events))
    assert patterns == []


def test_excluded_tools_are_skipped():
    """Universal/stateful tools shouldn't pollute the analysis."""
    events = []
    for sid in ("s1", "s2", "s3"):
        # todo_write is excluded; screen_capture is not.
        events.append(_ev(sid, "todo_write"))
        events.append(_ev(sid, "screen_capture"))
        events.append(_ev(sid, "image_read"))
    patterns = analyze_patterns(bus=_mock_bus(events))
    for p in patterns:
        assert "todo_write" not in p.tool_sequence


def test_longer_ngram_preferred_over_subset():
    """When A→B and A→B→C both qualify, prefer the longer one — it's
    more specific."""
    events = []
    for sid in ("s1", "s2", "s3", "s4"):
        events.append(_ev(sid, "a"))
        events.append(_ev(sid, "b"))
        events.append(_ev(sid, "c"))
    patterns = analyze_patterns(bus=_mock_bus(events))
    seqs = [p.tool_sequence for p in patterns]
    # A→B→C is in the result; A→B should be deduped out since it's a
    # contained subsequence with equal coverage.
    assert ("a", "b", "c") in seqs
    assert ("a", "b") not in seqs
    assert ("b", "c") not in seqs


def test_distinct_session_count_recorded():
    events = []
    for sid in ("alpha", "beta", "gamma", "delta"):
        events.append(_ev(sid, "search"))
        events.append(_ev(sid, "summarize"))
    patterns = analyze_patterns(bus=_mock_bus(events))
    target = next(
        (p for p in patterns
         if p.tool_sequence == ("search", "summarize")),
        None,
    )
    assert target is not None
    assert target.distinct_sessions == 4
    assert target.total_occurrences == 4
    # sample_session_ids is sorted + capped at 3
    assert len(target.sample_session_ids) == 3
    assert "alpha" in target.sample_session_ids


def test_total_occurrences_counts_all_instances():
    """Same session can contribute multiple occurrences but only one
    distinct_session — both numbers tracked separately."""
    events = []
    # session A: a→b twice
    events.append(_ev("A", "a"))
    events.append(_ev("A", "b"))
    events.append(_ev("A", "a"))
    events.append(_ev("A", "b"))
    # sessions B/C: a→b once each (to clear min_distinct=3)
    events.append(_ev("B", "a"))
    events.append(_ev("B", "b"))
    events.append(_ev("C", "a"))
    events.append(_ev("C", "b"))
    patterns = analyze_patterns(bus=_mock_bus(events))
    target = next(
        (p for p in patterns if p.tool_sequence == ("a", "b")),
        None,
    )
    assert target is not None
    assert target.distinct_sessions == 3
    assert target.total_occurrences == 4


# ── tuning knobs ─────────────────────────────────────────────────


def test_min_distinct_sessions_knob():
    events = [
        _ev("s1", "a"), _ev("s1", "b"),
        _ev("s2", "a"), _ev("s2", "b"),
    ]
    # default min=3 → filtered, but min=2 should find it
    p_default = analyze_patterns(bus=_mock_bus(events))
    assert p_default == []
    p_lower = analyze_patterns(
        bus=_mock_bus(events), min_distinct_sessions=2,
    )
    assert any(
        p.tool_sequence == ("a", "b") for p in p_lower
    )


def test_max_results_caps_output():
    events = []
    for sid in ("s1", "s2", "s3"):
        events.extend([
            _ev(sid, "p"), _ev(sid, "q"),
            _ev(sid, "r"), _ev(sid, "s"),
            _ev(sid, "t"), _ev(sid, "u"),
        ])
    patterns = analyze_patterns(
        bus=_mock_bus(events), max_results=3,
    )
    assert len(patterns) <= 3


# ── helpers ──────────────────────────────────────────────────────


def test_format_pattern_proposal_is_user_friendly():
    p = Pattern(
        tool_sequence=("screen_capture", "image_read"),
        distinct_sessions=4,
        total_occurrences=12,
    )
    msg = format_pattern_proposal(p)
    assert "screen_capture" in msg
    assert "image_read" in msg
    assert "→" in msg
    assert "4" in msg
    assert "12" in msg
    assert "skill" in msg


def test_subsequence_helper():
    long = Pattern(
        tool_sequence=("a", "b", "c", "d"),
        distinct_sessions=5,
        total_occurrences=10,
    )
    short_contained = Pattern(
        tool_sequence=("b", "c"),
        distinct_sessions=3,
        total_occurrences=4,
    )
    short_disjoint = Pattern(
        tool_sequence=("x", "y"),
        distinct_sessions=3,
        total_occurrences=4,
    )
    assert _is_subsequence_of_any(short_contained, [long]) is True
    assert _is_subsequence_of_any(short_disjoint, [long]) is False
