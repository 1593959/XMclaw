"""Prometheus-text-format ``/metrics`` endpoint — REMEDIATION_PLAN P1-3.

Aggregator subscribes to the in-process bus at lifespan-startup time
and updates a small set of in-memory counters / gauges / latency
histograms. The endpoint renders the current snapshot in
Prometheus text exposition format (v0.0.4) on every GET.

**Why no `prometheus_client` dep?** The library is ~3K LOC and
introduces a global metrics registry that fights with our
"daemon-instance-scoped state" lifecycle. The text format is 200
lines of spec; hand-rolling stays trivial. If we ever need
push-gateway / OpenTelemetry / multi-process support, swap then.

**Coverage:**

  - ``xmclaw_daemon_uptime_seconds`` — gauge
  - ``xmclaw_turns_total`` — counter (counted on USER_MESSAGE)
  - ``xmclaw_llm_requests_total`` — counter
  - ``xmclaw_llm_response_latency_seconds`` — histogram (LLM_REQUEST
    → LLM_RESPONSE wall-clock); 8 buckets (250ms…30s) plus +Inf
  - ``xmclaw_tool_invocations_total{name="...",ok="true|false"}``
    — counter labeled per tool name and outcome
  - ``xmclaw_cost_usd_total`` — counter, sums payload.delta_usd on
    COST_TICK
  - ``xmclaw_active_sessions`` — gauge, distinct session_ids seen
    in the last hour (5-min sliding window)

Every counter / histogram is **process-local and resets on
restart**. That's fine for liveness / SLO scraping; persistence
belongs in a dedicated time-series store, not in the daemon.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter
from starlette.responses import PlainTextResponse

from xmclaw.core.bus.events import BehavioralEvent, EventType


router = APIRouter(tags=["metrics"])


# ─── Histogram support ────────────────────────────────────────────


# Buckets in seconds — geometric-ish, suit LLM call latencies that
# typically sit in [0.5s, 30s] with a long tail.
_LATENCY_BUCKETS: tuple[float, ...] = (
    0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)


class _Histogram:
    """Minimal Prometheus-compatible histogram. Tracks per-bucket
    counts (cumulative), sum, and overall count."""

    __slots__ = ("buckets", "counts", "sum", "count")

    def __init__(self, buckets: tuple[float, ...] = _LATENCY_BUCKETS):
        self.buckets = buckets
        self.counts: list[int] = [0] * len(buckets)
        self.sum: float = 0.0
        self.count: int = 0

    def observe(self, value: float) -> None:
        for i, b in enumerate(self.buckets):
            if value <= b:
                self.counts[i] += 1
        self.sum += value
        self.count += 1

    def render_lines(self, name: str) -> list[str]:
        """Render Prometheus text-format lines for this histogram."""
        out: list[str] = []
        # Prometheus convention: buckets are cumulative — each bucket
        # includes all observations <= its upper bound. ``counts``
        # already holds cumulative counts (every observe() increments
        # every bucket whose ``le`` >= value).
        for i, b in enumerate(self.buckets):
            out.append(f'{name}_bucket{{le="{b}"}} {self.counts[i]}')
        out.append(f'{name}_bucket{{le="+Inf"}} {self.count}')
        out.append(f"{name}_sum {self.sum}")
        out.append(f"{name}_count {self.count}")
        return out


# ─── Aggregator (the single piece of state) ───────────────────────


class _MetricsAggregator:
    """Subscribes to the bus, accumulates counters/histograms.

    Lifecycle: one instance per daemon. ``app.state.metrics`` holds
    the reference; the lifespan wires ``bus.subscribe(predicate,
    handler)`` for each event we care about and tears down on
    shutdown (subscriptions cancel automatically when the bus does).
    """

    def __init__(self) -> None:
        self.started_at = time.time()
        self.turns_total = 0
        self.llm_requests_total = 0
        self.llm_latency = _Histogram()
        # tool counter keyed by (tool_name, ok-bool)
        self.tool_invocations: dict[tuple[str, bool], int] = defaultdict(int)
        self.cost_usd_total = 0.0
        # session_id → last_seen_ts (for "active in last 5 min" gauge)
        self.session_last_seen: dict[str, float] = {}
        # In-flight LLM requests for latency pairing.
        # Keyed by event.id of the LLM_REQUEST event.
        self._pending_llm_starts: dict[str, float] = {}

    # ── handlers (one per event type) ─────────────────────────────

    async def on_user_message(self, event: BehavioralEvent) -> None:
        self.turns_total += 1
        if event.session_id:
            self.session_last_seen[event.session_id] = time.time()

    async def on_llm_request(self, event: BehavioralEvent) -> None:
        self.llm_requests_total += 1
        # Stash start time keyed by event id. The matching response
        # carries ``payload["request_event_id"]`` (best-effort —
        # some translators don't echo it back, in which case we just
        # never pair and the latency histogram undercounts).
        self._pending_llm_starts[event.id] = event.ts

    async def on_llm_response(self, event: BehavioralEvent) -> None:
        # Match on payload["request_event_id"] if present; otherwise
        # the latency histogram silently skips this response.
        ref = (event.payload or {}).get("request_event_id")
        if not isinstance(ref, str):
            return
        start = self._pending_llm_starts.pop(ref, None)
        if start is None:
            return
        elapsed = max(0.0, event.ts - start)
        self.llm_latency.observe(elapsed)

    async def on_tool_finished(self, event: BehavioralEvent) -> None:
        payload = event.payload or {}
        name = str(payload.get("tool") or payload.get("name") or "unknown")
        ok = bool(payload.get("ok", True))
        self.tool_invocations[(name, ok)] += 1

    async def on_cost_tick(self, event: BehavioralEvent) -> None:
        payload = event.payload or {}
        delta = payload.get("delta_usd")
        if not isinstance(delta, (int, float)):
            # Fall back to "total accumulated" snapshot styles.
            delta = payload.get("usd") or 0.0
        try:
            self.cost_usd_total += max(0.0, float(delta))
        except (TypeError, ValueError):
            pass

    # ── render ────────────────────────────────────────────────────

    def active_sessions(self, window_s: float = 300.0) -> int:
        cutoff = time.time() - window_s
        return sum(
            1 for ts in self.session_last_seen.values() if ts >= cutoff
        )

    def render(self) -> str:
        lines: list[str] = []

        # ── daemon_uptime ─────
        lines.append(
            "# HELP xmclaw_daemon_uptime_seconds Seconds since this "
            "daemon's lifespan startup completed."
        )
        lines.append("# TYPE xmclaw_daemon_uptime_seconds gauge")
        lines.append(
            f"xmclaw_daemon_uptime_seconds {time.time() - self.started_at}"
        )

        # ── turns_total ─────
        lines.append(
            "# HELP xmclaw_turns_total Total user messages observed "
            "across all sessions since process start."
        )
        lines.append("# TYPE xmclaw_turns_total counter")
        lines.append(f"xmclaw_turns_total {self.turns_total}")

        # ── llm_requests_total ─────
        lines.append(
            "# HELP xmclaw_llm_requests_total Total LLM HTTP requests "
            "issued by the agent loop since process start."
        )
        lines.append("# TYPE xmclaw_llm_requests_total counter")
        lines.append(f"xmclaw_llm_requests_total {self.llm_requests_total}")

        # ── llm_response_latency ─────
        lines.append(
            "# HELP xmclaw_llm_response_latency_seconds Wall-clock "
            "latency between LLM_REQUEST and matching LLM_RESPONSE."
        )
        lines.append("# TYPE xmclaw_llm_response_latency_seconds histogram")
        lines.extend(
            self.llm_latency.render_lines("xmclaw_llm_response_latency_seconds")
        )

        # ── tool_invocations_total ─────
        lines.append(
            "# HELP xmclaw_tool_invocations_total Tool invocations "
            "completed, labeled by name and ok/failure."
        )
        lines.append("# TYPE xmclaw_tool_invocations_total counter")
        if self.tool_invocations:
            for (name, ok), n in sorted(self.tool_invocations.items()):
                # Escape only the bare minimum: backslash and quote per
                # Prometheus exposition spec. Tool names are usually
                # alphanumeric-with-underscore so this rarely fires.
                safe = name.replace("\\", "\\\\").replace('"', '\\"')
                ok_str = "true" if ok else "false"
                lines.append(
                    f'xmclaw_tool_invocations_total{{name="{safe}",ok="{ok_str}"}} {n}'
                )
        else:
            # Emit at least one sample so scrapers can confirm the
            # metric exists even when nothing has fired yet.
            lines.append('xmclaw_tool_invocations_total{name="",ok="true"} 0')

        # ── cost_usd_total ─────
        lines.append(
            "# HELP xmclaw_cost_usd_total Cumulative LLM spend in USD "
            "since process start."
        )
        lines.append("# TYPE xmclaw_cost_usd_total counter")
        lines.append(f"xmclaw_cost_usd_total {self.cost_usd_total}")

        # ── active_sessions ─────
        lines.append(
            "# HELP xmclaw_active_sessions Sessions with a user "
            "message in the last 5 minutes."
        )
        lines.append("# TYPE xmclaw_active_sessions gauge")
        lines.append(f"xmclaw_active_sessions {self.active_sessions()}")

        return "\n".join(lines) + "\n"


# ─── Wiring helper for the lifespan ───────────────────────────────


def install_metrics_subscriptions(bus: Any, agg: _MetricsAggregator) -> None:
    """Subscribe ``agg``'s handlers to the right event types on
    ``bus``. Called once during lifespan startup."""
    bus.subscribe(
        lambda e: e.type == EventType.USER_MESSAGE, agg.on_user_message,
    )
    bus.subscribe(
        lambda e: e.type == EventType.LLM_REQUEST, agg.on_llm_request,
    )
    bus.subscribe(
        lambda e: e.type == EventType.LLM_RESPONSE, agg.on_llm_response,
    )
    bus.subscribe(
        lambda e: e.type == EventType.TOOL_INVOCATION_FINISHED,
        agg.on_tool_finished,
    )
    bus.subscribe(
        lambda e: e.type == EventType.COST_TICK, agg.on_cost_tick,
    )


# ─── HTTP endpoint ────────────────────────────────────────────────


@router.get("/metrics")
async def get_metrics(request: Any) -> PlainTextResponse:
    """Prometheus exposition. ``Content-Type`` must be
    ``text/plain; version=0.0.4`` so Prometheus / VictoriaMetrics
    / Grafana Agent parse the body. No auth — bind 127.0.0.1 only
    (the daemon does by default; reverse-proxy users wire their
    own auth)."""
    agg = getattr(request.app.state, "metrics", None)
    if agg is None:
        body = (
            "# HELP xmclaw_metrics_disabled Metrics aggregator not wired.\n"
            "# TYPE xmclaw_metrics_disabled gauge\n"
            "xmclaw_metrics_disabled 1\n"
        )
    else:
        body = agg.render()
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


__all__ = [
    "router",
    "_MetricsAggregator",
    "install_metrics_subscriptions",
]
