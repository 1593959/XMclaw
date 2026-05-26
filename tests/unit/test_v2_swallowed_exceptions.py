"""Tests for the swallowed-exception counter + doctor check.

Locks the behavior:
* record() bumps a counter keyed on (scope, exc_class).
* snapshot / hottest / total expose the counters.
* reset() clears everything.
* SwallowedExceptionsCheck flags scope+class pairs above threshold.
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.utils.swallowed_exceptions import (
    hottest,
    record,
    reset,
    snapshot,
    total,
)


def test_record_increments_counter() -> None:
    reset()
    try:
        record("test.scope", ValueError("x"))
        record("test.scope", ValueError("y"))
        record("test.scope", KeyError("z"))
        assert total() == 3
        snap = snapshot()
        assert snap["test.scope:ValueError"] == 2
        assert snap["test.scope:KeyError"] == 1
    finally:
        reset()


def test_hottest_orders_by_count() -> None:
    reset()
    try:
        for _ in range(5):
            record("hot", ValueError(""))
        for _ in range(2):
            record("warm", KeyError(""))
        record("cold", IndexError(""))
        top = hottest(limit=10)
        assert top[0] == ("hot", "ValueError", 5)
        assert top[1] == ("warm", "KeyError", 2)
        assert top[2] == ("cold", "IndexError", 1)
    finally:
        reset()


def test_reset_clears_everything() -> None:
    record("x", ValueError(""))
    reset()
    assert total() == 0
    assert snapshot() == {}
    assert hottest() == []


# ── doctor check ──────────────────────────────────────────────────


def test_doctor_check_passes_when_below_threshold() -> None:
    from xmclaw.cli.doctor_registry import (
        DoctorContext,
        SwallowedExceptionsCheck,
    )
    reset()
    try:
        # 4 swallows on one scope, threshold is 5 → still OK.
        for _ in range(4):
            record("safe.scope", ValueError(""))
        ctx = DoctorContext(config_path=Path("/tmp/cfg"))
        result = SwallowedExceptionsCheck().run(ctx)
        assert result.ok is True
        assert "below threshold" in result.detail
    finally:
        reset()


def test_doctor_check_fails_when_over_threshold() -> None:
    from xmclaw.cli.doctor_registry import (
        DoctorContext,
        SwallowedExceptionsCheck,
    )
    reset()
    try:
        # 6 swallows on one scope → over threshold (5).
        for _ in range(6):
            record("flaky.scope", NameError(""))
        ctx = DoctorContext(config_path=Path("/tmp/cfg"))
        result = SwallowedExceptionsCheck().run(ctx)
        assert result.ok is False
        assert "flaky.scope" in result.detail
        assert "NameError" in result.detail
        assert result.advisory is not None
    finally:
        reset()


def test_doctor_check_passes_when_empty() -> None:
    from xmclaw.cli.doctor_registry import (
        DoctorContext,
        SwallowedExceptionsCheck,
    )
    reset()
    ctx = DoctorContext(config_path=Path("/tmp/cfg"))
    result = SwallowedExceptionsCheck().run(ctx)
    assert result.ok is True
    assert "no swallowed exceptions" in result.detail
