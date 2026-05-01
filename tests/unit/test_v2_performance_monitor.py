"""PerformanceMonitor unit tests.

This module had 0% coverage (170 uncovered statements) when we ran the
first audit; this suite brings it above 95%. Covers:

  - OperationStats math (avg, error_rate edge cases)
  - LLM / tool / agent / event tracking
  - Conversation lifecycle (start → end → summary) incl. history cap
  - Statistics retrieval: specific key, all keys, unknown key
  - Full report shape
  - Reset clears everything
  - OperationTimer context manager (three routing prefixes + exception path)
  - get_performance_monitor() singleton returns the same instance twice
"""
from __future__ import annotations

import pytest

from xmclaw.core.performance_monitor import (
    OperationStats,
    OperationTimer,
    PerformanceMonitor,
    get_performance_monitor,
)


# ── OperationStats math ─────────────────────────────────────────────────


def test_operation_stats_avg_is_zero_when_no_count() -> None:
    assert OperationStats().avg_duration == 0.0


def test_operation_stats_avg_duration() -> None:
    s = OperationStats(count=4, total_duration=8.0)
    assert s.avg_duration == 2.0


def test_operation_stats_error_rate_zero_when_no_count() -> None:
    assert OperationStats().error_rate == 0.0


def test_operation_stats_error_rate_basic() -> None:
    s = OperationStats(count=10, error_count=3)
    assert s.error_rate == 0.3


# ── LLM tracking ────────────────────────────────────────────────────────


def test_track_llm_call_accumulates_count_and_tokens() -> None:
    m = PerformanceMonitor()
    m.track_llm_call("anthropic", "claude-haiku", 1.5, True,
                     prompt_tokens=100, completion_tokens=50)
    m.track_llm_call("anthropic", "claude-haiku", 0.5, True,
                     prompt_tokens=200, completion_tokens=75)
    llm = m.get_llm_stats("anthropic:claude-haiku")
    assert llm["count"] == 2
    assert llm["avg_duration_ms"] == 1000.0   # mean of 1.5s and 0.5s
    assert llm["min_duration_ms"] == 500.0
    assert llm["max_duration_ms"] == 1500.0

    tok = m.get_token_usage()
    assert tok["anthropic:claude-haiku"]["prompt_tokens"] == 300
    assert tok["anthropic:claude-haiku"]["completion_tokens"] == 125
    assert tok["anthropic:claude-haiku"]["total_tokens"] == 425


def test_track_llm_call_failure_bumps_error_count() -> None:
    m = PerformanceMonitor()
    m.track_llm_call("openai", "gpt-4o", 2.0, False,
                     prompt_tokens=10, completion_tokens=0)
    stats = m.get_llm_stats("openai:gpt-4o")
    assert stats["count"] == 1
    assert stats["error_count"] == 1
    assert stats["error_rate"] == 1.0


def test_track_llm_error_standalone() -> None:
    """track_llm_error bumps error_count without affecting count --
    useful for error-before-completion scenarios."""
    m = PerformanceMonitor()
    m.track_llm_error("anthropic", "claude")
    stats = m.get_llm_stats("anthropic:claude")
    assert stats["error_count"] == 1
    # count stayed 0 -> error_rate is 0 (div-by-zero guard fired).
    assert stats["error_rate"] == 0.0


# ── Tool tracking ───────────────────────────────────────────────────────


def test_track_tool_call_records_per_tool_stats() -> None:
    m = PerformanceMonitor()
    m.track_tool_call("file_read", 0.02, True)
    m.track_tool_call("file_read", 0.04, True)
    m.track_tool_call("file_read", 0.01, False)
    s = m.get_tool_stats("file_read")
    assert s["count"] == 3
    assert s["error_count"] == 1
    # Error rate = 1/3 rounded to 3 decimals.
    assert s["error_rate"] == pytest.approx(0.333, abs=1e-3)


def test_get_tool_stats_unknown_returns_empty() -> None:
    assert PerformanceMonitor().get_tool_stats("does_not_exist") == {}


def test_get_tool_stats_no_arg_returns_all() -> None:
    m = PerformanceMonitor()
    m.track_tool_call("a", 0.1, True)
    m.track_tool_call("b", 0.2, True)
    all_stats = m.get_tool_stats()
    assert set(all_stats.keys()) == {"a", "b"}


# ── Agent tracking ──────────────────────────────────────────────────────


def test_track_agent_turn_records_stats_and_tool_events() -> None:
    m = PerformanceMonitor()
    m.track_agent_turn("agent-1", 5.0, ["file_read", "bash"], success=True)
    ag = m.get_agent_stats("agent-1")
    assert ag["count"] == 1
    events = m.get_event_summary()
    assert events["tool:file_read"] == 1
    assert events["tool:bash"] == 1


def test_agent_stats_aggregate_across_turns() -> None:
    m = PerformanceMonitor()
    m.track_agent_turn("a", 1.0, [], True)
    m.track_agent_turn("a", 3.0, [], False)
    s = m.get_agent_stats("a")
    assert s["count"] == 2
    assert s["min_duration_ms"] == 1000.0
    assert s["max_duration_ms"] == 3000.0
    assert s["error_count"] == 1


def test_get_agent_stats_unknown() -> None:
    assert PerformanceMonitor().get_agent_stats("nobody") == {}


# ── Event counts ────────────────────────────────────────────────────────


def test_track_event_counts() -> None:
    m = PerformanceMonitor()
    m.track_event("llm_response")
    m.track_event("llm_response")
    m.track_event("tool_call")
    assert m.get_event_summary() == {"llm_response": 2, "tool_call": 1}


# ── Conversation lifecycle ──────────────────────────────────────────────


def test_conversation_end_without_start_is_handled() -> None:
    """end_conversation on an unknown conversation_id shouldn't crash;
    the record just has empty/default agent_id."""
    m = PerformanceMonitor()
    m.track_conversation_end("never-started", success=True, turns=0, total_duration=0)
    summary = m.get_conversation_summary()
    assert summary["total_conversations"] == 1


def test_conversation_lifecycle_recorded_in_summary() -> None:
    m = PerformanceMonitor()
    m.track_conversation_start("conv-1", "agent-x")
    m.track_conversation_end("conv-1", success=True, turns=3, total_duration=6.0)
    m.track_conversation_start("conv-2", "agent-x")
    m.track_conversation_end("conv-2", success=False, turns=1, total_duration=2.0)
    s = m.get_conversation_summary()
    assert s["total_conversations"] == 2
    assert s["successful_conversations"] == 1
    assert s["success_rate"] == 0.5
    assert s["avg_duration_seconds"] == 4.0  # (6+2)/2
    assert s["avg_turns_per_conversation"] == 2.0
    assert s["total_turns"] == 4


def test_conversation_summary_is_zero_baseline_when_empty() -> None:
    m = PerformanceMonitor()
    s = m.get_conversation_summary()
    assert s == {"total_conversations": 0, "avg_duration": 0, "avg_turns": 0}


def test_conversation_history_capped_at_1000() -> None:
    """History trims to the most recent 1000 entries."""
    m = PerformanceMonitor()
    for i in range(1050):
        m.track_conversation_end(f"c-{i}", success=True, turns=1, total_duration=1.0)
    assert len(m._conversation_history) == 1000
    # Oldest 50 dropped; the final one is still there.
    assert m._conversation_history[-1]["conversation_id"] == "c-1049"


def test_conversation_avg_turn_duration_zero_when_zero_turns() -> None:
    m = PerformanceMonitor()
    m.track_conversation_end("c", success=True, turns=0, total_duration=0.0)
    assert m._conversation_history[-1]["avg_turn_duration"] == 0


# ── Full report + reset ─────────────────────────────────────────────────


def test_full_report_shape() -> None:
    m = PerformanceMonitor()
    m.track_llm_call("a", "b", 1.0, True, 1, 1)
    m.track_tool_call("x", 0.1, True)
    m.track_agent_turn("ag", 0.5, [], True)
    m.track_event("evt")
    m.track_conversation_start("c", "ag")
    m.track_conversation_end("c", True, turns=1, total_duration=0.5)
    report = m.get_full_report()
    assert set(report.keys()) == {
        "timestamp", "llm", "tools", "agents", "tokens", "events", "conversations",
    }
    assert report["events"]["evt"] == 1


def test_reset_stats_clears_all_categories() -> None:
    m = PerformanceMonitor()
    m.track_llm_call("a", "b", 1.0, True, 5, 5)
    m.track_tool_call("x", 0.1, True)
    m.track_agent_turn("ag", 0.5, [], True)
    m.track_event("e")
    m.track_conversation_start("c", "ag")
    m.track_conversation_end("c", True, 1, 0.5)
    m.reset_stats()
    assert m.get_llm_stats() == {}
    assert m.get_tool_stats() == {}
    assert m.get_agent_stats() == {}
    assert m.get_token_usage() == {}
    assert m.get_event_summary() == {}
    assert m.get_conversation_summary()["total_conversations"] == 0


# ── OperationTimer ──────────────────────────────────────────────────────


def test_timer_routes_llm_prefix_to_track_llm_call() -> None:
    m = PerformanceMonitor()
    with m.track_operation("llm:anthropic:claude-haiku"):
        pass
    stats = m.get_llm_stats()
    assert "anthropic:claude-haiku" in stats
    assert stats["anthropic:claude-haiku"]["count"] == 1


def test_timer_routes_tool_prefix_to_track_tool_call() -> None:
    m = PerformanceMonitor()
    with m.track_operation("tool:file_read"):
        pass
    assert m.get_tool_stats("file_read")["count"] == 1


def test_timer_routes_agent_prefix_to_track_agent_turn() -> None:
    m = PerformanceMonitor()
    with m.track_operation("agent:my-agent"):
        pass
    assert m.get_agent_stats("my-agent")["count"] == 1


def test_timer_records_failure_when_context_raises() -> None:
    m = PerformanceMonitor()
    with pytest.raises(RuntimeError):
        with m.track_operation("tool:bash"):
            raise RuntimeError("boom")
    # Failure was recorded even though the exception propagates.
    stats = m.get_tool_stats("bash")
    assert stats["count"] == 1
    assert stats["error_count"] == 1


def test_timer_unknown_prefix_is_noop() -> None:
    """A prefix that doesn't match llm:/tool:/agent: quietly does nothing."""
    m = PerformanceMonitor()
    with m.track_operation("weird:category"):
        pass
    assert m.get_llm_stats() == {}
    assert m.get_tool_stats() == {}
    assert m.get_agent_stats() == {}


def test_timer_direct_construction_works_too() -> None:
    """OperationTimer is usable without the helper method as well."""
    m = PerformanceMonitor()
    with OperationTimer(m, "tool:direct"):
        pass
    assert m.get_tool_stats("direct")["count"] == 1


# ── Global singleton ────────────────────────────────────────────────────


def test_get_performance_monitor_returns_singleton() -> None:
    a = get_performance_monitor()
    b = get_performance_monitor()
    assert a is b


def test_token_usage_cost_estimate_is_tracked() -> None:
    m = PerformanceMonitor()
    m.track_llm_call("a", "b", 0.1, True, prompt_tokens=1000, completion_tokens=0)
    usage = m.get_token_usage()
    assert usage["a:b"]["cost_estimate_usd"] == 0.01   # 1000 * 0.01/1k
