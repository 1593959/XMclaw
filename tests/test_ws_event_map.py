"""Pins the WS event-name contract (PR-E0-3, fixes audit finding M52).

The frontend dispatches on a stable wire `type` name. Regressions here would
silently blackhole evolution / reflection UI updates — exactly the class of
bug this contract was introduced to fix. Every evolution event MUST have a
mapping; adding a new EventType without updating WS_EVENT_MAP should require
an intentional decision (and a test update).
"""
from __future__ import annotations

from xmclaw.core.event_bus import EventType, WS_EVENT_MAP


def test_all_evolution_event_types_are_mapped():
    missing = [
        et.name for et in EventType
        if et.name.startswith("EVOLUTION_") and et.value not in WS_EVENT_MAP
    ]
    assert not missing, (
        f"Evolution EventTypes not in WS_EVENT_MAP: {missing}. "
        "Either map them to a wire name or justify the exception."
    )


def test_journal_state_machine_events_are_mapped():
    # Phase E0 journal events — the Evolution Live panel depends on these.
    required = [
        EventType.EVOLUTION_CYCLE_STARTED,
        EventType.EVOLUTION_REFLECTING,
        EventType.EVOLUTION_FORGING,
        EventType.EVOLUTION_VALIDATING,
        EventType.EVOLUTION_ARTIFACT_SHADOW,
        EventType.EVOLUTION_ARTIFACT_PROMOTED,
        EventType.EVOLUTION_ARTIFACT_RETIRED,
        EventType.EVOLUTION_ROLLBACK,
        EventType.EVOLUTION_REJECTED,
        EventType.EVOLUTION_CYCLE_ENDED,
    ]
    for et in required:
        assert et.value in WS_EVENT_MAP, f"{et.name} missing from WS_EVENT_MAP"


def test_wire_names_are_snake_case_and_unique():
    seen: set[str] = set()
    for wire in WS_EVENT_MAP.values():
        assert wire == wire.lower(), f"wire name {wire!r} is not lowercase"
        assert ":" not in wire, (
            f"wire name {wire!r} should use snake_case, not colon separator"
        )
        assert wire not in seen, f"duplicate wire name: {wire}"
        seen.add(wire)


def test_reflection_complete_has_stable_wire_name():
    # Frontend handler relies on 'reflection_complete' — pin it.
    assert WS_EVENT_MAP[EventType.REFLECTION_COMPLETE.value] == "reflection_complete"


def test_no_reserved_wire_name_collision():
    """'event' is the legacy envelope type — no mapped event may reuse it."""
    assert "event" not in WS_EVENT_MAP.values()
