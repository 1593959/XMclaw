"""Tests for xmclaw.security.auditor.SecurityAuditor."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from xmclaw.security.auditor import SecurityAuditor


@pytest.fixture
def auditor(tmp_path: Path):
    db = tmp_path / "security_audit.db"
    a = SecurityAuditor(db_path=db)
    yield a
    a.close()


# ── basic record / query ────────────────────────────────────────────────


def test_record_and_query(auditor: SecurityAuditor) -> None:
    auditor.record(
        event_type="prompt_injection",
        severity="high",
        source="tool_result",
        session_id="s1",
        details={"categories": ["instruction_override"]},
        acted=True,
    )
    rows = auditor.recent_events(limit=10)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "prompt_injection"
    assert rows[0]["severity"] == "high"
    assert rows[0]["acted"] is True
    assert rows[0]["details"]["categories"] == ["instruction_override"]


def test_recent_events_filter_by_session(auditor: SecurityAuditor) -> None:
    auditor.record(event_type="a", session_id="s1")
    auditor.record(event_type="b", session_id="s2")
    auditor.record(event_type="c", session_id="s1")
    rows = auditor.recent_events(session_id="s1")
    assert len(rows) == 2
    assert {r["event_type"] for r in rows} == {"a", "c"}


def test_recent_events_filter_by_type(auditor: SecurityAuditor) -> None:
    auditor.record(event_type="prompt_injection")
    auditor.record(event_type="anti_req_violation")
    auditor.record(event_type="prompt_injection")
    rows = auditor.recent_events(event_type="prompt_injection")
    assert len(rows) == 2


def test_recent_events_filter_by_since(auditor: SecurityAuditor) -> None:
    auditor.record(event_type="old")
    time.sleep(0.01)
    after = time.time()
    auditor.record(event_type="new")
    rows = auditor.recent_events(since=after)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "new"


# ── convenience wrappers ────────────────────────────────────────────────


def test_record_prompt_injection(auditor: SecurityAuditor) -> None:
    auditor.record_prompt_injection(
        session_id="s1",
        source="tool_result",
        policy="block",
        categories=["instruction_override", "role_forgery"],
        acted=True,
        scanned_length=1200,
    )
    rows = auditor.recent_events()
    assert rows[0]["event_type"] == "prompt_injection"
    assert rows[0]["severity"] == "high"
    assert rows[0]["details"]["policy"] == "block"
    assert rows[0]["details"]["scanned_length"] == 1200


def test_record_tool_guard_deny(auditor: SecurityAuditor) -> None:
    auditor.record_tool_guard(
        session_id="s1",
        tool_name="bash",
        action="deny",
        findings=[
            {"rule_id": "R1", "severity": "critical", "description": "rm -rf /"}
        ],
    )
    rows = auditor.recent_events()
    assert rows[0]["event_type"] == "tool_guard"
    assert rows[0]["severity"] == "critical"
    assert rows[0]["tool_name"] == "bash"
    assert rows[0]["details"]["action"] == "deny"


def test_record_tool_guard_approve(auditor: SecurityAuditor) -> None:
    auditor.record_tool_guard(
        session_id="s1",
        tool_name="file_delete",
        action="approve",
        findings=[],
    )
    rows = auditor.recent_events()
    assert rows[0]["severity"] == "high"
    assert rows[0]["acted"] is True


def test_record_anti_req(auditor: SecurityAuditor) -> None:
    auditor.record_anti_req(
        session_id="s1",
        kind="stuck_loop",
        message="agent stuck",
        tool_name="apply_patch",
        hop=5,
    )
    rows = auditor.recent_events()
    assert rows[0]["event_type"] == "anti_req_violation"
    assert rows[0]["severity"] == "high"
    assert rows[0]["details"]["hop"] == 5


def test_record_approval(auditor: SecurityAuditor) -> None:
    auditor.record_approval(
        session_id="s1",
        tool_name="bash",
        request_id="req-123",
        status="created",
        findings_summary="rm -rf detected",
    )
    rows = auditor.recent_events()
    assert rows[0]["event_type"] == "approval_created"
    assert rows[0]["tool_name"] == "bash"


# ── bus subscription (async) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_to_bus_prompt_injection(auditor: SecurityAuditor) -> None:
    from xmclaw.core.bus import EventType, make_event
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    auditor.subscribe_to_bus(bus)

    await bus.publish(make_event(
        session_id="s1",
        agent_id="agent-1",
        type=EventType.PROMPT_INJECTION_DETECTED,
        payload={
            "source": "tool_result",
            "policy": "block",
            "categories": ["instruction_override"],
            "acted": True,
            "scanned_length": 500,
        },
    ))
    await bus.drain()

    rows = auditor.recent_events(event_type="prompt_injection")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["acted"] is True


@pytest.mark.asyncio
async def test_subscribe_to_bus_anti_req(auditor: SecurityAuditor) -> None:
    from xmclaw.core.bus import EventType, make_event
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    auditor.subscribe_to_bus(bus)

    await bus.publish(make_event(
        session_id="s2",
        agent_id="agent-1",
        type=EventType.ANTI_REQ_VIOLATION,
        payload={
            "kind": "stuck_loop",
            "message": "agent stuck in loop",
            "tool": "bash",
            "hop": 3,
        },
    ))
    await bus.drain()

    rows = auditor.recent_events(event_type="anti_req_violation")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s2"
    assert rows[0]["details"]["kind"] == "stuck_loop"


@pytest.mark.asyncio
async def test_subscribe_ignores_unrelated_events(auditor: SecurityAuditor) -> None:
    from xmclaw.core.bus import EventType, make_event
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    auditor.subscribe_to_bus(bus)

    await bus.publish(make_event(
        session_id="s3",
        agent_id="agent-1",
        type=EventType.LLM_RESPONSE,
        payload={"content": "hello"},
    ))
    await bus.drain()

    rows = auditor.recent_events()
    assert len(rows) == 0


# ── resilience ──────────────────────────────────────────────────────────


def test_record_never_raises_even_with_bad_details(auditor: SecurityAuditor) -> None:
    """Audit-log trouble must never propagate to the caller."""
    # Details with a non-JSON-serialisable object would normally explode.
    class _Bad:
        pass

    # Actually json.dumps of arbitrary objects raises TypeError.
    # The record() method catches all exceptions.
    auditor.record(event_type="x", details={"bad": _Bad()})  # type: ignore[arg-type]
    # If we get here without an exception, the swallow worked.
    assert True
