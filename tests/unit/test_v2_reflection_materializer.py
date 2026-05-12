"""Unit tests for ReflectionMaterializer — close-the-loop end-to-end.

Pre-this materializer, agent reflections (INNER_MONOLOGUE + R3
METACOGNITION_PROPOSAL) had no Python consumer — they sat in events.db
for UI display only. These tests pin the contract that publishing a
``plan`` thought or a ``preference_update`` proposal results in a
fresh bullet under the right persona-file section so the next turn's
system-prompt build picks it up automatically.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus, make_event


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def persona_dir(tmp_path: Path) -> Path:
    d = tmp_path / "default"
    d.mkdir()
    return d


@pytest.fixture
def materializer(persona_dir: Path):
    """Build a started materializer + bus pair. Stops in teardown."""
    from xmclaw.cognition.reflection_materializer import (
        ReflectionMaterializer,
    )
    bus = InProcessEventBus()
    rm = ReflectionMaterializer(
        bus=bus,
        persona_dir_provider=lambda: persona_dir,
    )
    return bus, rm


# ── Inner-monologue paths ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_thought_lands_in_agents_md(
    materializer, persona_dir: Path,
):
    bus, rm = materializer
    await rm.start()

    ev = make_event(
        session_id="_system",
        agent_id="reflection",
        type=EventType.INNER_MONOLOGUE,
        payload={
            "kind": "plan",
            "text": "Next time the user complains about slow chat, "
                    "check Ollama embedding latency first.",
            "tick": 5,
            "trigger": "session_lifecycle_burst",
        },
    )
    await bus.publish(ev)
    await bus.drain()

    agents_md = (persona_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Auto-extracted reflections" in agents_md
    assert "slow chat" in agents_md
    assert "Ollama embedding latency" in agents_md
    # Date stamp shape (YYYY-MM-DD)
    import re
    assert re.search(r"- 20\d{2}-\d{2}-\d{2}: ", agents_md)
    await rm.stop()


@pytest.mark.asyncio
async def test_concern_thought_lands_in_memory_md(
    materializer, persona_dir: Path,
):
    bus, rm = materializer
    await rm.start()

    await bus.publish(make_event(
        session_id="_system",
        agent_id="reflection",
        type=EventType.INNER_MONOLOGUE,
        payload={
            "kind": "concern",
            "text": "User keeps hitting WS disconnect before sending "
                    "any message — auth might be misconfigured.",
        },
    ))
    await bus.drain()

    memory_md = (persona_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Failure Modes" in memory_md
    assert "WS disconnect" in memory_md
    await rm.stop()


@pytest.mark.asyncio
async def test_observation_and_wonder_thoughts_skipped(
    materializer, persona_dir: Path,
):
    """Descriptive thoughts (observation / wonder / hypothesis) are
    NOT materialised — only ``plan`` + ``concern`` are actionable.
    Observations would balloon AGENTS.md with running commentary."""
    bus, rm = materializer
    await rm.start()

    for kind in ("observation", "wonder", "hypothesis", "reflection"):
        await bus.publish(make_event(
            session_id="_system",
            agent_id="reflection",
            type=EventType.INNER_MONOLOGUE,
            payload={"kind": kind, "text": f"a {kind} thought"},
        ))
    await bus.drain()

    # Persona dir is empty — neither AGENTS.md nor MEMORY.md was created.
    assert not (persona_dir / "AGENTS.md").exists()
    assert not (persona_dir / "MEMORY.md").exists()
    await rm.stop()


# ── Metacognition-proposal paths ──────────────────────────────────


@pytest.mark.asyncio
async def test_preference_update_lands_in_user_md(
    materializer, persona_dir: Path,
):
    bus, rm = materializer
    await rm.start()

    await bus.publish(make_event(
        session_id="_system",
        agent_id="metacognition",
        type=EventType.METACOGNITION_PROPOSAL,
        payload={
            "kind": "preference_update",
            "confidence": 0.45,
            "why": "user pushed back 4× on long responses",
            "payload": {"rule": "Default to terse answers; expand only on request."},
        },
    ))
    await bus.drain()

    user_md = (persona_dir / "USER.md").read_text(encoding="utf-8")
    assert "## Auto-extracted preferences" in user_md
    assert "terse answers" in user_md
    await rm.stop()


@pytest.mark.asyncio
async def test_curriculum_edit_lands_in_agents_md(
    materializer, persona_dir: Path,
):
    bus, rm = materializer
    await rm.start()

    await bus.publish(make_event(
        session_id="_system",
        agent_id="metacognition",
        type=EventType.METACOGNITION_PROPOSAL,
        payload={
            "kind": "curriculum_edit",
            "confidence": 0.55,
            "why": "agent forgets to read CLAUDE.md before editing",
            "payload": {
                "rule": "Before editing files under xmclaw/, "
                        "read the local AGENTS.md first.",
            },
        },
    ))
    await bus.drain()

    agents_md = (persona_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Auto-extracted curriculum" in agents_md
    assert "AGENTS.md first" in agents_md
    await rm.stop()


@pytest.mark.asyncio
async def test_low_confidence_proposal_gated(
    materializer, persona_dir: Path,
):
    """preference_update requires confidence ≥ 0.3 by default;
    below that we don't pollute USER.md with weak signals."""
    bus, rm = materializer
    await rm.start()

    await bus.publish(make_event(
        session_id="_system",
        agent_id="metacognition",
        type=EventType.METACOGNITION_PROPOSAL,
        payload={
            "kind": "preference_update",
            "confidence": 0.15,  # below 0.3 floor
            "why": "weak hunch",
            "payload": {"rule": "Should not land."},
        },
    ))
    await bus.drain()

    assert not (persona_dir / "USER.md").exists()
    await rm.stop()


@pytest.mark.asyncio
async def test_skill_propose_not_materialized_here(
    materializer, persona_dir: Path,
):
    """skill_propose flows through ProposalMaterializer (writes
    SKILL.md). This module deliberately does NOT also write to a
    persona file for skill_propose — would dual-author the same
    artifact."""
    bus, rm = materializer
    await rm.start()

    await bus.publish(make_event(
        session_id="_system",
        agent_id="metacognition",
        type=EventType.METACOGNITION_PROPOSAL,
        payload={
            "kind": "skill_propose",
            "confidence": 0.55,
            "why": "agent repeats the same 5-step bash dance",
            "payload": {
                "skill_id": "git-quickcommit",
                "draft": {"body": "..."},
            },
        },
    ))
    await bus.drain()

    # None of the persona files were touched.
    for f in ("USER.md", "AGENTS.md", "MEMORY.md", "TOOLS.md", "SOUL.md", "LEARNING.md"):
        assert not (persona_dir / f).exists(), (
            f"{f} should NOT be touched by skill_propose"
        )
    await rm.stop()


# ── Rate-limit + cap ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_caps_plan_thoughts(
    materializer, persona_dir: Path,
):
    """Quota is per-kind sliding-window. After 4 plan thoughts in a
    row, the 5th must NOT land. Without this, a chatty LLM can flood
    AGENTS.md inside one reflection cycle."""
    bus, rm = materializer
    await rm.start()

    for i in range(6):
        await bus.publish(make_event(
            session_id="_system",
            agent_id="reflection",
            type=EventType.INNER_MONOLOGUE,
            payload={
                "kind": "plan",
                "text": f"Plan number {i}: do something specific {i}.",
            },
        ))
    await bus.drain()

    agents_md = (persona_dir / "AGENTS.md").read_text(encoding="utf-8")
    # Count occurrences of our marker phrase — must be ≤ 4 per the cap.
    landed = agents_md.count("Plan number")
    assert landed <= 4, (
        f"Expected ≤ 4 plans landed under quota, got {landed}"
    )
    assert landed >= 1, "Expected at least one to land"
    await rm.stop()


# ── Off-switch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_does_not_subscribe(persona_dir: Path):
    """``cognition.reflection_materialize.enabled=false`` → no
    subscription, no writes. Mirrors the gate every other materializer
    in the daemon exposes."""
    from xmclaw.cognition.reflection_materializer import (
        ReflectionMaterializer,
    )
    bus = InProcessEventBus()
    rm = ReflectionMaterializer(
        bus=bus,
        persona_dir_provider=lambda: persona_dir,
        cfg={"cognition": {"reflection_materialize": {"enabled": False}}},
    )
    await rm.start()

    await bus.publish(make_event(
        session_id="_system",
        agent_id="reflection",
        type=EventType.INNER_MONOLOGUE,
        payload={"kind": "plan", "text": "should NOT land"},
    ))
    await bus.drain()

    assert not (persona_dir / "AGENTS.md").exists()
    await rm.stop()


# ── Cache invalidation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_freeze_generation_bumped_on_write(
    materializer, persona_dir: Path,
):
    """The whole point of writing to persona files is that the NEXT
    turn picks them up. The system-prompt cache must invalidate after
    each write so AgentLoop doesn't keep serving the pre-reflection
    prompt forever."""
    from xmclaw.daemon import prompt_builder
    bus, rm = materializer
    await rm.start()
    before = prompt_builder._PROMPT_FREEZE_GENERATION

    await bus.publish(make_event(
        session_id="_system",
        agent_id="reflection",
        type=EventType.INNER_MONOLOGUE,
        payload={
            "kind": "plan",
            "text": "A new plan emerges from this turn's reflection.",
        },
    ))
    await bus.drain()

    after = prompt_builder._PROMPT_FREEZE_GENERATION
    assert after > before, (
        "Materializer must bump prompt-freeze generation after a "
        f"successful write (before={before}, after={after})"
    )
    await rm.stop()
