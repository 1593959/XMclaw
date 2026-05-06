"""Smoke tests for the analytics router — cost estimation +
top_errors aggregation + markdown export.

The router's heavy lifting is SQL aggregation against events.db
(skipped here — that's covered indirectly by integration probes).
These tests pin the public-facing helpers and the markdown shape.
"""
from __future__ import annotations

import json
import re

import pytest

from xmclaw.daemon.routers.analytics import (
    _estimate_cost_usd,
    _platform_of,
    get_analytics,
    get_analytics_markdown,
)


# ── Cost heuristic ──────────────────────────────────────────────────


def test_cost_claude_sonnet() -> None:
    """Sonnet-class: $3/Mtok input, $15/Mtok output."""
    cost = _estimate_cost_usd("claude-3-sonnet-20240229", 1_000_000, 500_000)
    # 1.0 * 3 + 0.5 * 15 = 3 + 7.5 = 10.5
    assert abs(cost - 10.5) < 0.01


def test_cost_gpt4o() -> None:
    cost = _estimate_cost_usd("gpt-4o-2024-11-20", 1_000_000, 1_000_000)
    # 2.5 + 10 = 12.5
    assert abs(cost - 12.5) < 0.01


def test_cost_kimi_uses_kimi_rate() -> None:
    """Substring match on 'kimi' picks the Chinese-provider rate."""
    cost = _estimate_cost_usd("kimi-k2.6", 1_000_000, 1_000_000)
    # 0.30 + 1.20 = 1.5
    assert abs(cost - 1.5) < 0.01


def test_cost_unknown_model_uses_default() -> None:
    cost = _estimate_cost_usd("some-random-local-model", 1_000_000, 1_000_000)
    # default 0.5 + 1.5 = 2.0
    assert abs(cost - 2.0) < 0.01


def test_cost_zero_tokens_zero_cost() -> None:
    assert _estimate_cost_usd("claude", 0, 0) == 0.0


def test_cost_empty_model_doesnt_crash() -> None:
    assert _estimate_cost_usd("", 1000, 1000) > 0
    assert _estimate_cost_usd(None, 1000, 1000) > 0  # type: ignore[arg-type]


# ── Platform classification ────────────────────────────────────────


def test_platform_classification() -> None:
    assert _platform_of("chat-abc123") == "web"
    assert _platform_of("feishu:user.name") == "feishu"
    assert _platform_of("reflect:turn-7") == "reflect"
    assert _platform_of("dream:cycle-3") == "dream"
    assert _platform_of("skill-dream:c1") == "dream"
    assert _platform_of("probe-foo") == "probe"
    assert _platform_of("flow-bar") == "probe"
    assert _platform_of("test-baz") == "probe"
    assert _platform_of("unknown") == "other"
    assert _platform_of(None) == "other"
    assert _platform_of("") == "other"


# ── Endpoint smoke ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analytics_endpoint_with_no_db(tmp_path, monkeypatch) -> None:
    """When events.db doesn't exist (fresh install / new test env), the
    endpoint must return a valid empty rollup, not crash."""
    from xmclaw.daemon.routers import analytics as ana_mod
    monkeypatch.setattr(ana_mod, "default_events_db_path",
                        lambda: tmp_path / "nope.db")
    resp = await get_analytics(days=7)
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert body["period_days"] == 7
    assert body["summary"]["total_calls"] == 0
    # P0 wrap-up: cost + failed-call summaries should be present even
    # in the empty case.
    assert body["summary"].get("total_cost_usd") == 0
    assert body["summary"].get("total_failed_calls") == 0
    assert body["top_errors"] == []
    assert body["models"] == []


@pytest.mark.asyncio
async def test_markdown_endpoint_renders_overview(tmp_path, monkeypatch) -> None:
    """``/api/v2/analytics/report.md`` should produce a valid Markdown
    document even when there's no data — empty tables still render."""
    from xmclaw.daemon.routers import analytics as ana_mod
    monkeypatch.setattr(ana_mod, "default_events_db_path",
                        lambda: tmp_path / "nope.db")
    resp = await get_analytics_markdown(days=14)
    text = resp.body.decode("utf-8")
    assert "# XMclaw Analytics (14 days)" in text
    assert "## Overview" in text
    assert "## Models" in text
    assert "## Tools" in text
    assert "## Platforms" in text
    # ``Generated:`` line should have the correct date stamp shape
    assert re.search(r"Generated:.*UTC", text)
