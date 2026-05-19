"""Epic #27 sweep #15 (2026-05-19): daily_digest croniter-less fallback.

Pre-fix, when croniter wasn't importable AND the configured schedule
was a full cron expression (e.g. ``"0 22 * * *"``), the trigger set
``_next_fire_ts = None`` permanently and ``should_fire`` returned
False forever — the daily_digest feature has been silently dead on
many installs since launch. New behavior: fall back to a 24h
interval so the digest still fires once a day; log a one-shot
warning suggesting ``pip install croniter`` to restore the
configured time-of-day.
"""
from __future__ import annotations

from typing import Any

import pytest

from xmclaw.cognition.triggers_digest import DailyDigestTrigger


class _FakeBus:
    """Minimal bus that records nothing — DailyDigestTrigger doesn't
    publish; it returns proposals via ``propose()``."""

    async def publish(self, _evt: Any) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_falls_back_to_interval_when_croniter_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron-expression schedule + no croniter → falls back to
    ``every 1d`` instead of permanently disabling the trigger."""
    # Force the cron-import probe to fail.
    monkeypatch.setattr(
        "xmclaw.core.scheduler.cron._try_import_croniter",
        lambda: None,
    )
    trig = DailyDigestTrigger(
        bus=_FakeBus(),
        schedule_expr="0 22 * * *",
        lookback_h=24.0,
    )
    # Pre-fix: _next_fire_ts is None and trigger never fires.
    # New: fallback computed _next_fire_ts ~24h out, _used_interval_fallback True.
    assert trig._next_fire_ts is not None
    assert trig._used_interval_fallback is True


@pytest.mark.asyncio
async def test_keeps_cron_when_croniter_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When croniter IS importable, the configured cron expression
    is honoured — no fallback fires."""
    # If the env actually has croniter, the test just verifies the
    # happy path. If not, skip — we shouldn't pretend croniter is
    # installed when it's not (monkeypatching croniter back in here
    # would require a stub).
    try:
        import croniter  # noqa: F401
    except ImportError:
        pytest.skip("croniter not installed in this env")
    trig = DailyDigestTrigger(
        bus=_FakeBus(),
        schedule_expr="0 22 * * *",
        lookback_h=24.0,
    )
    assert trig._next_fire_ts is not None
    assert trig._used_interval_fallback is False


@pytest.mark.asyncio
async def test_pure_interval_schedule_unaffected() -> None:
    """``every 1d`` style schedules work without croniter at all —
    the fallback path is only relevant for cron-expression schedules.
    Configured intervals pass straight through."""
    trig = DailyDigestTrigger(
        bus=_FakeBus(),
        schedule_expr="every 1d",
        lookback_h=24.0,
    )
    assert trig._next_fire_ts is not None
    # Not a fallback — the original expression worked directly.
    assert trig._used_interval_fallback is False
