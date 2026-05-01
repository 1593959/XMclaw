"""Journal — unit tests (Epic #24 Phase 2.1).

Locks the contract:

* ``JournalWriter`` subscribes idempotently and writes one JSONL row
  per ``SESSION_LIFECYCLE phase=destroy``.
* Mechanical fields are populated correctly from the event stream
  (turn_count from USER_MESSAGE, tool_calls from
  TOOL_INVOCATION_FINISHED, grader stats from GRADER_VERDICT,
  anti_req from ANTI_REQ_VIOLATION).
* ``JournalReader`` reads exactly the files the writer wrote (same
  path, no shadow indexes).
* ``stop()`` flushes still-open buffers so a daemon SIGINT doesn't
  drop in-flight rows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.journal import JournalEntry, JournalReader, JournalWriter


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "journal"


async def _publish(bus: InProcessEventBus, **kwargs) -> None:
    """Convenience wrapper for ``bus.publish(make_event(**kwargs))``."""
    await bus.publish(make_event(**kwargs))


# ── basic write ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_destroy_event_flushes_one_row(
    bus: InProcessEventBus, root: Path,
) -> None:
    w = JournalWriter(bus, root=root)
    await w.start()
    try:
        await _publish(
            bus, session_id="s1", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create"},
        )
        await _publish(
            bus, session_id="s1", agent_id="agent",
            type=EventType.USER_MESSAGE,
            payload={"content": "hi"},
        )
        await _publish(
            bus, session_id="s1", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "destroy"},
        )
        await bus.drain()
    finally:
        await w.stop()

    entries = JournalReader(root=root).recent()
    assert len(entries) == 1
    e = entries[0]
    assert e.session_id == "s1"
    assert e.turn_count == 1
    assert e.duration_s >= 0.0


# ── mechanical fields ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mechanical_fields_populated_from_event_stream(
    bus: InProcessEventBus, root: Path,
) -> None:
    w = JournalWriter(bus, root=root)
    await w.start()
    try:
        await _publish(
            bus, session_id="s2", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create"},
        )
        # 3 user turns
        for _ in range(3):
            await _publish(
                bus, session_id="s2", agent_id="agent",
                type=EventType.USER_MESSAGE, payload={"content": "x"},
            )
        # 2 tool calls — one OK, one error
        await _publish(
            bus, session_id="s2", agent_id="agent",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={"name": "bash", "ok": True, "error": None},
        )
        await _publish(
            bus, session_id="s2", agent_id="agent",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={"name": "file_read", "ok": False, "error": "ENOENT"},
        )
        # 3 grader verdicts
        for s in (0.95, 0.50, 0.10):
            await _publish(
                bus, session_id="s2", agent_id="agent",
                type=EventType.GRADER_VERDICT,
                payload={"score": s, "ran": True, "returned": True,
                         "type_matched": True, "side_effect_observable": None,
                         "evidence": []},
            )
        # 1 anti-req
        await _publish(
            bus, session_id="s2", agent_id="agent",
            type=EventType.ANTI_REQ_VIOLATION,
            payload={"message": "max_hops"},
        )
        await _publish(
            bus, session_id="s2", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "destroy"},
        )
        await bus.drain()
    finally:
        await w.stop()

    [e] = JournalReader(root=root).recent()
    assert e.turn_count == 3
    assert len(e.tool_calls) == 2
    assert e.tool_calls[0].name == "bash"
    assert e.tool_calls[0].ok is True
    assert e.tool_calls[1].name == "file_read"
    assert e.tool_calls[1].ok is False
    assert e.tool_calls[1].error == "ENOENT"
    assert e.grader_play_count == 3
    assert e.grader_lowest == pytest.approx(0.10)
    assert e.grader_highest == pytest.approx(0.95)
    assert e.grader_avg_score == pytest.approx((0.95 + 0.50 + 0.10) / 3)
    assert e.anti_req_violations == 1


# ── reader sees writer's path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reader_path_equals_writer_path(
    bus: InProcessEventBus, root: Path,
) -> None:
    """User's 2026-05-01 anti-req: write path == read path."""
    w = JournalWriter(bus, root=root)
    await w.start()
    try:
        await _publish(
            bus, session_id="abc", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create"},
        )
        await _publish(
            bus, session_id="abc", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "destroy"},
        )
        await bus.drain()
    finally:
        await w.stop()

    # Reader uses the SAME root, returns the SAME entry.
    r = JournalReader(root=root)
    assert r.root == root
    by_sid = r.by_session_id("abc")
    assert len(by_sid) == 1
    assert by_sid[0].session_id == "abc"


# ── stop() flushes pending buffers ───────────────────────────────────


@pytest.mark.asyncio
async def test_stop_flushes_in_flight_sessions(
    bus: InProcessEventBus, root: Path,
) -> None:
    """If daemon shuts down mid-session, the journal row should still
    land (truncated ts_end) rather than evaporating."""
    w = JournalWriter(bus, root=root)
    await w.start()
    await _publish(
        bus, session_id="never_destroyed", agent_id="agent",
        type=EventType.SESSION_LIFECYCLE,
        payload={"phase": "create"},
    )
    await _publish(
        bus, session_id="never_destroyed", agent_id="agent",
        type=EventType.USER_MESSAGE,
        payload={"content": "hi"},
    )
    await bus.drain()

    # No destroy event → still in buffer.
    assert JournalReader(root=root).recent() == []

    await w.stop()  # should flush

    [e] = JournalReader(root=root).recent()
    assert e.session_id == "never_destroyed"
    assert e.turn_count == 1


# ── start is idempotent ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_idempotent(bus: InProcessEventBus, root: Path) -> None:
    w = JournalWriter(bus, root=root)
    await w.start()
    await w.start()  # second call no-op
    assert w.is_running()
    await w.stop()
    assert not w.is_running()


# ── jsonl format round-trip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_jsonl_row_round_trips(bus: InProcessEventBus, root: Path) -> None:
    w = JournalWriter(bus, root=root)
    await w.start()
    try:
        await _publish(
            bus, session_id="rt", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create"},
        )
        await _publish(
            bus, session_id="rt", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "destroy"},
        )
        await bus.drain()
    finally:
        await w.stop()

    # Find the file directly + parse manually.
    files = list(root.rglob("*.jsonl"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8").strip()
    parsed = JournalEntry.from_jsonable(json.loads(raw))
    [from_reader] = JournalReader(root=root).recent()
    assert parsed == from_reader


# ── filename safety ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_id_with_path_chars_safe(
    bus: InProcessEventBus, root: Path,
) -> None:
    """A session_id like 'discord:dm:12345' must not create subdirs.

    The Path traversal hardening matches QwenPaw's session-id sanitize
    pattern (memory: project_peer_pain_points 2026-04-21)."""
    w = JournalWriter(bus, root=root)
    await w.start()
    try:
        sid = "discord:dm:12345/../foo\\bar"
        await _publish(
            bus, session_id=sid, agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create"},
        )
        await _publish(
            bus, session_id=sid, agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "destroy"},
        )
        await bus.drain()
    finally:
        await w.stop()

    files = list(root.rglob("*.jsonl"))
    # Exactly one file, no traversal escape.
    assert len(files) == 1
    # Path must be inside root.
    assert root in files[0].parents
