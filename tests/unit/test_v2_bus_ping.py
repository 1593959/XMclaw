"""Smoke test for the v2 event bus end-to-end.

If this fails the entire v2 skeleton is broken — nothing else should run.
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    make_event,
)
from xmclaw.core.bus.memory import accept_all


@pytest.mark.asyncio
async def test_publish_subscribe_roundtrip() -> None:
    bus = InProcessEventBus()
    received: list[BehavioralEvent] = []

    async def handler(event: BehavioralEvent) -> None:
        received.append(event)

    bus.subscribe(accept_all, handler)

    ev = make_event(
        session_id="s",
        agent_id="a",
        type=EventType.USER_MESSAGE,
        payload={"content": "hello"},
    )
    await bus.publish(ev)
    await bus.drain()

    assert len(received) == 1
    assert received[0].id == ev.id
    assert received[0].payload["content"] == "hello"


@pytest.mark.asyncio
async def test_predicate_filters_events() -> None:
    bus = InProcessEventBus()
    seen: list[BehavioralEvent] = []

    async def handler(event: BehavioralEvent) -> None:
        seen.append(event)

    bus.subscribe(lambda e: e.type == EventType.USER_MESSAGE, handler)

    await bus.publish(make_event(
        session_id="s", agent_id="a", type=EventType.USER_MESSAGE, payload={},
    ))
    await bus.publish(make_event(
        session_id="s", agent_id="a", type=EventType.COST_TICK, payload={},
    ))
    await bus.drain()

    assert len(seen) == 1
    assert seen[0].type == EventType.USER_MESSAGE


@pytest.mark.asyncio
async def test_subscriber_exception_isolates() -> None:
    bus = InProcessEventBus()
    good_seen: list[BehavioralEvent] = []

    async def bad(_event: BehavioralEvent) -> None:
        raise RuntimeError("boom")

    async def good(event: BehavioralEvent) -> None:
        good_seen.append(event)

    bus.subscribe(accept_all, bad)
    bus.subscribe(accept_all, good)

    await bus.publish(make_event(
        session_id="s", agent_id="a", type=EventType.USER_MESSAGE, payload={},
    ))
    await bus.drain()

    # Good subscriber still received the event despite bad subscriber crashing.
    assert len(good_seen) == 1


@pytest.mark.asyncio
async def test_subscriber_exception_is_logged_structurally(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Epic #15 phase 2: subscriber failures must go to the structured
    logger (not ``print``) so the secret scrubber runs and operators can
    filter by event_type / session_id.

    We assert at the rendered-output level (capsys) rather than caplog,
    because setup_logging is not necessarily called in unit tests —
    structlog's default factory emits to stdout. What matters for the
    regression guard is "the message carries the structured event name
    and key=value pairs", not the specific routing backend.
    """
    bus = InProcessEventBus()

    async def bad(_event: BehavioralEvent) -> None:
        raise RuntimeError("kaboom-with-secrets")

    bus.subscribe(accept_all, bad)
    ev = make_event(
        session_id="sess-xyz", agent_id="a",
        type=EventType.USER_MESSAGE, payload={},
    )
    await bus.publish(ev)
    await bus.drain()

    captured = capsys.readouterr()
    output = captured.out + captured.err
    # Structured tag — proves it went through a logger, not raw print.
    assert "bus.subscriber_failed" in output
    # Context fields preserved — proves operators can grep by session.
    assert "sess-xyz" in output
    assert "kaboom-with-secrets" in output
    # Negative: the old print-based format had '[bus] subscriber failed'.
    # If that string ever reappears we've regressed to print().
    assert "[bus] subscriber failed" not in output
