"""Epic #27 sweep #10 (2026-05-19) — HoldoutTestSignal real wiring
via :mod:`xmclaw.core.grader.holdout_registry`.

Pins:
  * Register / lookup / unregister / clear roundtrip.
  * Overwriting an existing registration logs a warning (does NOT
    silently lose the warning).
  * Bad input (empty id, non-callable) raises ValueError up-front.
  * run_check accepts sync + async callables, swallows exceptions
    (returns None), coerces 0/1 → False/True.
  * HoldoutTestSignal.probe pulls from the registry, with the
    ``holdout_test_passed`` override still taking precedence
    (backward compat for existing tests).
  * Unregistered eval_test_id → None (signal not applicable),
    NOT a fake 0/1.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Mapping

import pytest

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.core.grader._signals import HoldoutTestSignal
from xmclaw.core.grader.holdout_registry import (
    clear,
    lookup,
    register,
    registered_ids,
    run_check,
    unregister,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test starts with an empty global registry."""
    clear()
    yield
    clear()


# ── registry CRUD ──────────────────────────────────────────────────


def test_register_and_lookup() -> None:
    def check(payload: Mapping) -> bool:  # noqa: ARG001
        return True

    register("xmc.test.always_true", check)
    assert lookup("xmc.test.always_true") is check
    assert "xmc.test.always_true" in registered_ids()


def test_lookup_returns_none_for_unknown() -> None:
    assert lookup("nonexistent") is None
    # Non-string is also defensive: None, not raise.
    assert lookup(123) is None  # type: ignore[arg-type]


def test_unregister_returns_true_when_present() -> None:
    register("xmc.test.foo", lambda _payload: True)
    assert unregister("xmc.test.foo") is True
    assert lookup("xmc.test.foo") is None
    # Idempotent — second call returns False.
    assert unregister("xmc.test.foo") is False


def test_clear_drops_all() -> None:
    register("a", lambda _payload: True)
    register("b", lambda _payload: False)
    assert len(registered_ids()) == 2
    clear()
    assert registered_ids() == []


def test_register_rejects_empty_id() -> None:
    with pytest.raises(ValueError):
        register("", lambda _payload: True)
    with pytest.raises(ValueError):
        register("   ", lambda _payload: True)


def test_register_rejects_non_callable() -> None:
    with pytest.raises(ValueError):
        register("foo", "not a callable")  # type: ignore[arg-type]


def test_register_overwrite_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    register("xmc.test.dup", lambda _payload: True)
    with caplog.at_level(logging.WARNING, logger="xmclaw.core.grader.holdout_registry"):
        register("xmc.test.dup", lambda _payload: False)
    assert any(
        "overwriting" in r.message.lower()
        for r in caplog.records
    )


# ── run_check ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_check_sync_callable_true() -> None:
    register("xmc.test.t", lambda _payload: True)
    assert await run_check("xmc.test.t", {}) is True


@pytest.mark.asyncio
async def test_run_check_sync_callable_false() -> None:
    register("xmc.test.f", lambda _payload: False)
    assert await run_check("xmc.test.f", {}) is False


@pytest.mark.asyncio
async def test_run_check_async_callable() -> None:
    async def acheck(_payload: Mapping) -> bool:
        await asyncio.sleep(0)
        return True
    register("xmc.test.async", acheck)
    assert await run_check("xmc.test.async", {}) is True


@pytest.mark.asyncio
async def test_run_check_unregistered_returns_none() -> None:
    assert await run_check("nope", {}) is None


@pytest.mark.asyncio
async def test_run_check_swallows_exceptions() -> None:
    def bad(_payload: Mapping) -> bool:
        raise RuntimeError("boom")
    register("xmc.test.bad", bad)
    # Buggy verify hook must NOT crash the grader.
    assert await run_check("xmc.test.bad", {}) is None


@pytest.mark.asyncio
async def test_run_check_swallows_async_exceptions() -> None:
    async def bad(_payload: Mapping) -> bool:
        raise ValueError("async boom")
    register("xmc.test.async_bad", bad)
    assert await run_check("xmc.test.async_bad", {}) is None


@pytest.mark.asyncio
async def test_run_check_coerces_truthy_int_returns() -> None:
    """A callable returning 1 / 0 (legitimate Python truthy) is
    accepted as True / False — but anything else (None, dict, str)
    is treated as 'no verdict' (None)."""
    register("xmc.test.one", lambda _payload: 1)
    register("xmc.test.zero", lambda _payload: 0)
    register("xmc.test.none", lambda _payload: None)  # type: ignore[arg-type,return-value]
    register("xmc.test.str", lambda _payload: "yes")  # type: ignore[arg-type,return-value]
    assert await run_check("xmc.test.one", {}) is True
    assert await run_check("xmc.test.zero", {}) is False
    assert await run_check("xmc.test.none", {}) is None
    assert await run_check("xmc.test.str", {}) is None


@pytest.mark.asyncio
async def test_run_check_receives_payload() -> None:
    """The callable gets the event payload — it's how checks
    inspect the post-state (e.g. ``payload['output_path']``)."""
    seen: dict = {}

    def hook(payload: Mapping) -> bool:
        seen.update(payload)
        return True
    register("xmc.test.peek", hook)
    await run_check("xmc.test.peek", {"output_path": "/tmp/x"})
    assert seen.get("output_path") == "/tmp/x"


# ── HoldoutTestSignal integration ─────────────────────────────────


def _make_event(payload: dict) -> BehavioralEvent:
    import time
    import uuid
    return BehavioralEvent(
        id=str(uuid.uuid4()),
        ts=time.time(),
        session_id="test-session",
        agent_id="test-agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_signal_returns_score_when_registry_passes() -> None:
    register("xmc.test.outcome", lambda _payload: True)
    signal = HoldoutTestSignal()
    score, meta = await signal.probe(
        _make_event({"eval_test_id": "xmc.test.outcome"}),
    )
    assert score == 1.0
    assert meta.get("source") == "registry"
    assert meta.get("passed") is True


@pytest.mark.asyncio
async def test_signal_returns_zero_when_registry_fails() -> None:
    register("xmc.test.outcome_bad", lambda _payload: False)
    signal = HoldoutTestSignal()
    score, meta = await signal.probe(
        _make_event({"eval_test_id": "xmc.test.outcome_bad"}),
    )
    assert score == 0.0
    assert meta.get("source") == "registry"


@pytest.mark.asyncio
async def test_signal_returns_none_for_unregistered_id() -> None:
    """Backward compat with the pre-sweep behaviour: when nothing's
    registered for the id, signal returns None (not applicable),
    not a fake 0 that would punish the skill."""
    signal = HoldoutTestSignal()
    score, meta = await signal.probe(
        _make_event({"eval_test_id": "never-registered"}),
    )
    assert score is None
    assert meta.get("status") == "unregistered"


@pytest.mark.asyncio
async def test_signal_returns_none_for_check_that_raises() -> None:
    def bad(_payload: Mapping) -> bool:
        raise RuntimeError("oops")
    register("xmc.test.broken", bad)
    signal = HoldoutTestSignal()
    score, meta = await signal.probe(
        _make_event({"eval_test_id": "xmc.test.broken"}),
    )
    assert score is None
    assert meta.get("status") == "check_raised"


@pytest.mark.asyncio
async def test_signal_no_eval_id_returns_none() -> None:
    """Events without eval_test_id are not applicable."""
    signal = HoldoutTestSignal()
    score, meta = await signal.probe(_make_event({}))
    assert score is None
    assert meta == {}


@pytest.mark.asyncio
async def test_signal_payload_override_still_works() -> None:
    """Backward compat: ``holdout_test_passed`` payload override
    short-circuits the registry lookup. Existing tests in
    test_v2_signals_holdout_cross relied on this path; preserve it."""
    signal = HoldoutTestSignal()
    score, meta = await signal.probe(_make_event({
        "eval_test_id": "anything",
        "holdout_test_passed": True,
    }))
    assert score == 1.0
    assert meta.get("source") == "payload_override"
