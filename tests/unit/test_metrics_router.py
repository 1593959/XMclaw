"""Tests for the Prometheus /metrics endpoint (P1-3)."""
from __future__ import annotations

import time

import pytest

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.daemon.routers.metrics import (
    _Histogram,
    _MetricsAggregator,
    install_metrics_subscriptions,
)


def _event(type_: EventType, payload: dict | None = None,
           session_id: str = "s1", event_id: str = "e1",
           ts: float | None = None) -> BehavioralEvent:
    return BehavioralEvent(
        id=event_id,
        type=type_,
        session_id=session_id,
        agent_id="agent",
        ts=ts if ts is not None else time.time(),
        payload=payload or {},
    )


# ─── Histogram ────────────────────────────────────────────────────


def test_histogram_observe_updates_count_and_sum():
    h = _Histogram(buckets=(0.5, 1.0, 5.0))
    h.observe(0.3)
    h.observe(0.8)
    h.observe(3.0)
    assert h.count == 3
    assert h.sum == pytest.approx(4.1)


def test_histogram_buckets_are_cumulative():
    h = _Histogram(buckets=(0.5, 1.0, 5.0))
    h.observe(0.3)   # falls into all 3 buckets
    h.observe(0.8)   # falls into top 2
    h.observe(3.0)   # falls into top 1
    # cumulative: 1 / 2 / 3
    assert h.counts == [1, 2, 3]


def test_histogram_render_emits_le_lines_plus_inf():
    h = _Histogram(buckets=(0.5, 1.0))
    h.observe(0.3)
    h.observe(0.7)
    lines = h.render_lines("xmclaw_x")
    text = "\n".join(lines)
    assert 'xmclaw_x_bucket{le="0.5"} 1' in text
    assert 'xmclaw_x_bucket{le="1.0"} 2' in text
    assert 'xmclaw_x_bucket{le="+Inf"} 2' in text
    assert "xmclaw_x_sum 1.0" in text
    assert "xmclaw_x_count 2" in text


# ─── Aggregator handlers ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_message_increments_turns_and_records_session():
    agg = _MetricsAggregator()
    await agg.on_user_message(_event(EventType.USER_MESSAGE, session_id="sX"))
    await agg.on_user_message(_event(EventType.USER_MESSAGE, session_id="sY"))
    await agg.on_user_message(_event(EventType.USER_MESSAGE, session_id="sX"))
    assert agg.turns_total == 3
    # Two distinct sessions seen.
    assert agg.active_sessions(window_s=3600) == 2


@pytest.mark.asyncio
async def test_active_sessions_window_drops_stale():
    agg = _MetricsAggregator()
    # Inject a session seen "long ago" by writing directly.
    agg.session_last_seen["old"] = time.time() - 7200  # 2h ago
    agg.session_last_seen["fresh"] = time.time()
    assert agg.active_sessions(window_s=300) == 1


@pytest.mark.asyncio
async def test_llm_request_response_pair_records_latency():
    agg = _MetricsAggregator()
    t0 = time.time()
    await agg.on_llm_request(_event(
        EventType.LLM_REQUEST, event_id="req-1", ts=t0,
    ))
    await agg.on_llm_response(_event(
        EventType.LLM_RESPONSE,
        payload={"request_event_id": "req-1"},
        ts=t0 + 0.42,
    ))
    assert agg.llm_requests_total == 1
    assert agg.llm_latency.count == 1
    assert agg.llm_latency.sum == pytest.approx(0.42, abs=1e-6)


@pytest.mark.asyncio
async def test_llm_response_without_request_event_id_is_dropped():
    """A translator that doesn't echo request_event_id back leaves
    latency unobserved — that's by design (better than guessing)."""
    agg = _MetricsAggregator()
    await agg.on_llm_request(_event(
        EventType.LLM_REQUEST, event_id="req-1", ts=time.time(),
    ))
    await agg.on_llm_response(_event(
        EventType.LLM_RESPONSE, payload={},  # no request_event_id
    ))
    assert agg.llm_latency.count == 0


@pytest.mark.asyncio
async def test_tool_invocation_counter_labels_by_name_and_ok():
    agg = _MetricsAggregator()
    await agg.on_tool_finished(_event(
        EventType.TOOL_INVOCATION_FINISHED,
        payload={"tool": "file_read", "ok": True},
    ))
    await agg.on_tool_finished(_event(
        EventType.TOOL_INVOCATION_FINISHED,
        payload={"tool": "file_read", "ok": True},
    ))
    await agg.on_tool_finished(_event(
        EventType.TOOL_INVOCATION_FINISHED,
        payload={"tool": "bash", "ok": False},
    ))
    assert agg.tool_invocations[("file_read", True)] == 2
    assert agg.tool_invocations[("bash", False)] == 1


@pytest.mark.asyncio
async def test_cost_tick_sums_delta_usd():
    agg = _MetricsAggregator()
    await agg.on_cost_tick(_event(
        EventType.COST_TICK, payload={"delta_usd": 0.012},
    ))
    await agg.on_cost_tick(_event(
        EventType.COST_TICK, payload={"delta_usd": 0.003},
    ))
    assert agg.cost_usd_total == pytest.approx(0.015, abs=1e-6)


@pytest.mark.asyncio
async def test_cost_tick_falls_back_to_usd_field():
    """Some publishers use ``usd`` instead of ``delta_usd``. Don't
    silently miss them."""
    agg = _MetricsAggregator()
    await agg.on_cost_tick(_event(
        EventType.COST_TICK, payload={"usd": 0.05},
    ))
    assert agg.cost_usd_total == pytest.approx(0.05, abs=1e-6)


# ─── Render (Prometheus exposition format) ────────────────────────


@pytest.mark.asyncio
async def test_render_emits_all_core_metrics():
    agg = _MetricsAggregator()
    await agg.on_user_message(_event(EventType.USER_MESSAGE, session_id="a"))
    await agg.on_tool_finished(_event(
        EventType.TOOL_INVOCATION_FINISHED,
        payload={"tool": "memory_search", "ok": True},
    ))
    await agg.on_cost_tick(_event(
        EventType.COST_TICK, payload={"delta_usd": 0.01},
    ))

    text = agg.render()

    # Every metric must have a HELP, a TYPE, and a sample line.
    for metric in (
        "xmclaw_daemon_uptime_seconds",
        "xmclaw_turns_total",
        "xmclaw_llm_requests_total",
        "xmclaw_llm_response_latency_seconds",
        "xmclaw_tool_invocations_total",
        "xmclaw_cost_usd_total",
        "xmclaw_active_sessions",
    ):
        assert f"# HELP {metric}" in text
        assert f"# TYPE {metric}" in text

    assert "xmclaw_turns_total 1" in text
    assert (
        'xmclaw_tool_invocations_total{name="memory_search",ok="true"} 1'
        in text
    )
    assert "xmclaw_active_sessions 1" in text


def test_render_with_empty_aggregator_still_valid_exposition():
    """A fresh daemon with no traffic must still emit a valid scrape
    body — Prometheus retries on parse errors and we don't want
    flap."""
    agg = _MetricsAggregator()
    text = agg.render()
    # Counter sentinel line for tool invocations.
    assert 'xmclaw_tool_invocations_total{name="",ok="true"} 0' in text
    # Body must end with a newline (exposition spec).
    assert text.endswith("\n")


def test_render_escapes_tool_label_quotes():
    """Tool names with quotes/backslashes would break the exposition
    if not escaped. Unlikely in practice but spec-mandated."""
    agg = _MetricsAggregator()
    agg.tool_invocations[('weird"name', True)] = 5
    text = agg.render()
    assert 'name="weird\\"name"' in text


# ─── Subscription wiring ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_metrics_subscriptions_wires_five_event_types():
    """End-to-end: subscribe via the real InProcessEventBus and verify
    that publishing each event type updates the right counter."""
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    agg = _MetricsAggregator()
    install_metrics_subscriptions(bus, agg)

    # Fan out one of each.
    t0 = time.time()
    await bus.publish(_event(EventType.USER_MESSAGE))
    await bus.publish(_event(EventType.LLM_REQUEST, event_id="r1", ts=t0))
    await bus.publish(_event(
        EventType.LLM_RESPONSE,
        payload={"request_event_id": "r1"},
        ts=t0 + 0.1,
    ))
    await bus.publish(_event(
        EventType.TOOL_INVOCATION_FINISHED,
        payload={"tool": "x", "ok": True},
    ))
    await bus.publish(_event(
        EventType.COST_TICK, payload={"delta_usd": 0.005},
    ))

    await bus.drain()

    assert agg.turns_total == 1
    assert agg.llm_requests_total == 1
    assert agg.llm_latency.count == 1
    assert agg.tool_invocations[("x", True)] == 1
    assert agg.cost_usd_total == pytest.approx(0.005, abs=1e-6)
