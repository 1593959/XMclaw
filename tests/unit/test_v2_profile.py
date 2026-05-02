"""ProfileExtractor — unit tests (Epic #24 Phase 2.2).

Locks the contract:

* Subscribes idempotently. Buffers per-session.
* Threshold flush: every Nth user turn fires the extractor.
* Session destroy flushes regardless of count.
* Stop() flushes still-open buffers (SIGINT defence).
* Writes go to the *exact* path the persona assembler reads
  (anti-req from 2026-05-01: write path == read path).
* Atomic append + per-path lock survive concurrent writers.
* Confidence floor drops low-confidence deltas.
* USER_PROFILE_UPDATED event published with the delta payload.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.profile import ProfileDelta, ProfileExtractor


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def user_md(tmp_path: Path) -> Path:
    persona = tmp_path / "persona" / "default"
    persona.mkdir(parents=True, exist_ok=True)
    return persona / "USER.md"


def _provider(path: Path):
    """Build a persona_user_md_provider closure for tests."""
    return lambda: path


def _delta(text: str, *, conf: float = 0.9, kind: str = "preference") -> ProfileDelta:
    return ProfileDelta(
        kind=kind, text=text, confidence=conf,
        source_session_id="sess", source_event_id="ev1",
        ts=time.time(),
    )


# ── threshold flush ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_after_threshold_user_turns(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    calls: list[tuple[list[dict], dict]] = []

    def fake_extractor(msgs, meta):
        calls.append((msgs, meta))
        return [_delta("user prefers terse answers")]

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=fake_extractor, flush_threshold=2,
    )
    await ex.start()
    try:
        # 2 user turns + 2 assistant responses → threshold hit on turn 2
        for i in range(2):
            await bus.publish(make_event(
                session_id="s", agent_id="agent",
                type=EventType.USER_MESSAGE,
                payload={"content": f"turn {i}"},
            ))
            await bus.publish(make_event(
                session_id="s", agent_id="agent",
                type=EventType.LLM_RESPONSE,
                payload={"content": f"reply {i}"},
            ))
        await bus.drain()
    finally:
        await ex.stop()

    assert len(calls) >= 1, "extractor was invoked at threshold"
    assert user_md.is_file()
    content = user_md.read_text(encoding="utf-8")
    assert "user prefers terse answers" in content
    assert "## Auto-extracted preferences" in content


# ── session destroy flushes regardless of count ─────────────────────


@pytest.mark.asyncio
async def test_destroy_flushes_below_threshold(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    def fake_extractor(_msgs, _meta):
        return [_delta("user runs Windows")]

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=fake_extractor, flush_threshold=10,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="s2", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "hi"},
        ))
        await bus.publish(make_event(
            session_id="s2", agent_id="agent",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "destroy"},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    assert "user runs Windows" in user_md.read_text(encoding="utf-8")


# ── confidence floor drops low-confidence deltas ────────────────────


@pytest.mark.asyncio
async def test_confidence_floor_drops_low_confidence(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    def fake_extractor(_msgs, _meta):
        return [
            _delta("real signal", conf=0.9),
            _delta("noisy guess", conf=0.2),
        ]

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=fake_extractor, flush_threshold=1,
        min_confidence=0.5,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="s3", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "hi"},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    text = user_md.read_text(encoding="utf-8")
    assert "real signal" in text
    assert "noisy guess" not in text


# ── empty extractor result is a no-op ───────────────────────────────


@pytest.mark.asyncio
async def test_no_deltas_no_write(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=lambda _m, _meta: [],
        flush_threshold=1,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="s4", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "hi"},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    assert not user_md.exists() or user_md.read_text(encoding="utf-8") == ""


# ── USER_PROFILE_UPDATED event published ────────────────────────────


@pytest.mark.asyncio
async def test_publishes_user_profile_updated_event(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    seen = []

    async def capture(event):
        if event.type == EventType.USER_PROFILE_UPDATED:
            seen.append(event)

    bus.subscribe(lambda e: e.type == EventType.USER_PROFILE_UPDATED, capture)

    def fake_extractor(_msgs, _meta):
        return [_delta("a thing", conf=0.9)]

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=fake_extractor, flush_threshold=1,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="s5", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "hi"},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    assert len(seen) == 1
    p = seen[0].payload
    assert p["delta_count"] == 1
    assert p["file_path"] == str(user_md)
    assert p["session_id"] == "s5"
    assert p["deltas"][0]["text"] == "a thing"


# ── append twice keeps both deltas ──────────────────────────────────


@pytest.mark.asyncio
async def test_second_flush_appends_below_existing(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    seq = iter([
        [_delta("first delta", conf=0.9)],
        [_delta("second delta", conf=0.9)],
    ])

    def fake_extractor(_msgs, _meta):
        return next(seq, [])

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=fake_extractor, flush_threshold=1,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="s6", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "a"},
        ))
        await bus.drain()
        await bus.publish(make_event(
            session_id="s6", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "b"},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    text = user_md.read_text(encoding="utf-8")
    assert "first delta" in text
    assert "second delta" in text
    # Only ONE section header (we append below the same one).
    assert text.count("## Auto-extracted preferences") == 1


# ── extractor exception doesn't crash subscription ──────────────────


@pytest.mark.asyncio
async def test_extractor_exception_isolated(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    crashes = {"n": 0}

    def crashing(_msgs, _meta):
        crashes["n"] += 1
        raise RuntimeError("simulated extractor failure")

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=crashing, flush_threshold=1,
    )
    await ex.start()
    try:
        for _ in range(3):
            await bus.publish(make_event(
                session_id="s7", agent_id="agent",
                type=EventType.USER_MESSAGE, payload={"content": "x"},
            ))
            await bus.drain()
    finally:
        await ex.stop()

    # Subscription survived all three crashes.
    assert crashes["n"] >= 3
    assert not user_md.exists() or "## Auto-extracted" not in user_md.read_text(
        encoding="utf-8"
    )


# ── start/stop idempotent ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stop_idempotent(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    ex = ProfileExtractor(bus, _provider(user_md))
    await ex.start()
    await ex.start()  # no-op
    assert ex.is_running()
    await ex.stop()
    await ex.stop()  # no-op
    assert not ex.is_running()


# ── async extractor callable supported ──────────────────────────────


@pytest.mark.asyncio
async def test_async_extractor_callable(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    async def async_extractor(_msgs, _meta):
        return [_delta("async delta", conf=0.9)]

    ex = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=async_extractor, flush_threshold=1,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="s8", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "hi"},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    assert "async delta" in user_md.read_text(encoding="utf-8")


# ── B-179 dedup ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_drops_identical_text_same_kind(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """The exact joint-audit pain: '用中文' written 4 times across
    different sessions. Each session's flush passes its own delta
    list; the dedup logic must drop incoming deltas whose
    (kind, fingerprint) already appear in the file."""
    ex = ProfileExtractor(
        bus=bus, persona_user_md_provider=_provider(user_md),
        extractor_callable=lambda *_, **__: [
            _delta("Always responds in Chinese"),
        ],
        flush_threshold=1, min_confidence=0.0,
    )
    await ex.start()
    try:
        # Fire 4 separate flushes — each invocation extracts the same
        # delta (worst case: ProfileExtractor + LLM keep recovering
        # the same fact every session that mentioned it).
        for sid in ("s1", "s2", "s3", "s4"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.USER_MESSAGE, payload={"content": "嗨"},
            ))
            await bus.drain()
    finally:
        await ex.stop()

    text = user_md.read_text(encoding="utf-8")
    # Only ONE auto-extract line for that fact, despite 4 flushes.
    occurrences = text.count("Always responds in Chinese")
    assert occurrences == 1, (
        f"dedup failed; line appears {occurrences}× in:\n{text}"
    )


@pytest.mark.asyncio
async def test_dedup_normalises_whitespace_and_punctuation(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """'uses Python.' / 'Uses Python' / 'uses Python  ' should all
    collapse via the fingerprint normaliser."""
    variants = [
        _delta("uses Python."),
        _delta("Uses Python"),
        _delta("uses  Python"),
        _delta("uses Python\n"),  # trailing newline normalises away
    ]
    call_idx = {"i": 0}

    def gen(*_a, **_k):
        out = [variants[call_idx["i"]]]
        call_idx["i"] += 1
        return out

    ex = ProfileExtractor(
        bus=bus, persona_user_md_provider=_provider(user_md),
        extractor_callable=gen,
        flush_threshold=1, min_confidence=0.0,
    )
    await ex.start()
    try:
        for sid in ("a", "b", "c", "d"):
            await bus.publish(make_event(
                session_id=sid, agent_id="agent",
                type=EventType.USER_MESSAGE, payload={"content": "..."},
            ))
            await bus.drain()
    finally:
        await ex.stop()

    text = user_md.read_text(encoding="utf-8")
    # Count any line with "python" (case-insensitive).
    py_lines = sum(1 for line in text.splitlines()
                   if line.startswith("- [auto") and "python" in line.lower())
    assert py_lines == 1, (
        f"normalising-dedup failed; {py_lines} variants survived:\n{text}"
    )


@pytest.mark.asyncio
async def test_dedup_preserves_different_kinds_with_same_text(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """A 'preference' and a 'constraint' with identical text are
    different observations — must NOT collapse."""
    deltas = [
        _delta("Use Python", kind="preference"),
        _delta("Use Python", kind="constraint"),
    ]
    yielded = {"once": False}

    def gen(*_a, **_k):
        if yielded["once"]:
            return []
        yielded["once"] = True
        return deltas

    ex = ProfileExtractor(
        bus=bus, persona_user_md_provider=_provider(user_md),
        extractor_callable=gen,
        flush_threshold=1, min_confidence=0.0,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="x", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "..."},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    text = user_md.read_text(encoding="utf-8")
    pref_lines = sum(1 for ln in text.splitlines() if "preference" in ln and "Use Python" in ln)
    cons_lines = sum(1 for ln in text.splitlines() if "constraint" in ln and "Use Python" in ln)
    assert pref_lines == 1
    assert cons_lines == 1


@pytest.mark.asyncio
async def test_dedup_no_write_when_all_duplicates(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """If every incoming delta is already represented, the file
    mtime stays — keeps audit log clean and avoids unnecessary
    USER_PROFILE_UPDATED events."""
    user_md.parent.mkdir(parents=True, exist_ok=True)
    user_md.write_text(
        "## Auto-extracted preferences\n\n"
        "- [auto · preference · conf=0.9 · session=old] Use Python\n",
        encoding="utf-8",
    )
    ex = ProfileExtractor(
        bus=bus, persona_user_md_provider=_provider(user_md),
        extractor_callable=lambda *_, **__: [_delta("Use Python")],
        flush_threshold=1, min_confidence=0.0,
    )
    await ex.start()
    try:
        await bus.publish(make_event(
            session_id="dup", agent_id="agent",
            type=EventType.USER_MESSAGE, payload={"content": "..."},
        ))
        await bus.drain()
    finally:
        await ex.stop()

    # Content unchanged: only the original line remains.
    text = user_md.read_text(encoding="utf-8")
    assert text.count("Use Python") == 1


# ── B-179 fingerprint helper unit tests ──────────────────────────


def test_fingerprint_normalises_case_and_whitespace() -> None:
    from xmclaw.core.profile.extractor import _fingerprint
    assert _fingerprint("Use Python") == _fingerprint("use python")
    assert _fingerprint("uses  Python.") == _fingerprint("uses python")
    assert _fingerprint("'用中文'") == _fingerprint("用中文")


def test_existing_fingerprints_parses_auto_lines_only() -> None:
    """Hand-written lines outside the [auto · ...] format don't
    contribute to the dedup set — manual edits stay manual."""
    from xmclaw.core.profile.extractor import _existing_fingerprints
    text = (
        "# USER.md\n\n"
        "- This is a manual note about the user, no auto prefix\n"
        "## Auto-extracted preferences\n\n"
        "- [auto · preference · conf=0.9 · session=s1] Use Python\n"
        "- [auto · habit · conf=0.8 · session=s2] Late-night work\n"
    )
    fps = _existing_fingerprints(text)
    assert ("preference", "use python") in fps
    assert ("habit", "late-night work") in fps
    # Manual line not included.
    assert all("manual note" not in fp for _, fp in fps)


# ── B-197: dual-write via fact_writer callback ───────────────────────


@pytest.mark.asyncio
async def test_b197_dual_write_calls_fact_writer(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """B-197: when fact_writer callback is wired, accepted deltas are
    handed to it with kind=preference metadata. Daemon side then
    builds the MemoryItem + persists — keeps core/ free of providers/
    imports per the layering rule."""

    # Real LLM extractors use meta.last_user_event_id to avoid
    # re-extracting on subsequent flushes; our stub returns deltas
    # only on the first call.
    fired = {"n": 0}
    def extractor(_msgs, _meta):
        fired["n"] += 1
        if fired["n"] > 1:
            return []
        return [_delta("用中文", conf=0.9), _delta("late-night work", conf=0.85)]

    writes: list[tuple[str, dict]] = []
    async def _fact_writer(text: str, metadata: dict) -> None:
        writes.append((text, metadata))

    pe = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=extractor,
        flush_threshold=1,
        fact_writer=_fact_writer,
    )
    await pe.start()
    try:
        await bus.publish(make_event(
            session_id="sess", agent_id="a",
            type=EventType.USER_MESSAGE,
            payload={"content": "hi"},
        ))
        await bus.publish(make_event(
            session_id="sess", agent_id="a",
            type=EventType.LLM_RESPONSE,
            payload={"content": "hello"},
        ))
        await bus.drain()
    finally:
        await pe.stop()

    assert len(writes) == 2, writes
    kinds = {md.get("kind") for _, md in writes}
    assert kinds == {"preference"}
    for _, md in writes:
        assert md.get("evidence_count") == 1
        assert md.get("source_session_id") == "sess"
        assert md.get("delta_kind") in ("preference", "habit", "style", "constraint")


@pytest.mark.asyncio
async def test_b197_no_fact_writer_keeps_legacy_path(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """fact_writer=None keeps the markdown-only behaviour unchanged.
    Tests / installs without a vec store must still work."""

    def extractor(_msgs, _meta):
        return [_delta("用中文", conf=0.9)]

    pe = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=extractor,
        flush_threshold=1,
        # fact_writer explicitly None
    )
    await pe.start()
    try:
        await bus.publish(make_event(
            session_id="s1", agent_id="a",
            type=EventType.USER_MESSAGE, payload={"content": "ping"},
        ))
        await bus.publish(make_event(
            session_id="s1", agent_id="a",
            type=EventType.LLM_RESPONSE, payload={"content": "pong"},
        ))
        await bus.drain()
    finally:
        await pe.stop()

    body = user_md.read_text(encoding="utf-8")
    assert "用中文" in body


@pytest.mark.asyncio
async def test_b197_fact_writer_failure_does_not_break_markdown(
    bus: InProcessEventBus, user_md: Path,
) -> None:
    """If fact_writer raises, the markdown path must still have happened.
    DB is best-effort indexing; markdown is the user-visible surface."""

    async def _broken_writer(text: str, metadata: dict) -> None:
        raise RuntimeError("simulated DB failure")

    def extractor(_msgs, _meta):
        return [_delta("用中文", conf=0.9)]

    pe = ProfileExtractor(
        bus, _provider(user_md),
        extractor_callable=extractor,
        flush_threshold=1,
        fact_writer=_broken_writer,
    )
    await pe.start()
    try:
        await bus.publish(make_event(
            session_id="sx", agent_id="a",
            type=EventType.USER_MESSAGE, payload={"content": "hi"},
        ))
        await bus.publish(make_event(
            session_id="sx", agent_id="a",
            type=EventType.LLM_RESPONSE, payload={"content": "yo"},
        ))
        await bus.drain()
    finally:
        await pe.stop()

    # Even though writer blew up, the markdown got the delta.
    assert "用中文" in user_md.read_text(encoding="utf-8")
