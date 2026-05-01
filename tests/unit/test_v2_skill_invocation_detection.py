"""B-122 — pin _detect_skill_invocations heuristic accuracy.

The detection used to do pure substring matching on skill_id, which
fired false positives whenever a snake_case skill_id ("git_status")
contained a token-wise common phrase ("git status"). Worse, when the
agent enumerated its skills (answering "what skills do you have?"),
every skill_id appeared in assistant_text and inflated the
invocation_count metric across the board.

Pins:

  * skill_id matches require word boundaries (\\b) — "git_status"
    skill no longer fires on "the git status command" in text
  * 3+ distinct skill_ids in assistant_text → enumeration mode,
    detection short-circuits and emits NOTHING
  * title min length raised 4→6 — a 4-char title like "code" or
    "test" no longer fires on every dev-loop turn
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCallShape, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.learned_skills import LearnedSkill
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


# ── minimal LLM stub (AgentLoop ctor needs one) ──────────────────────


@dataclass
class _NopLLM(LLMProvider):
    model: str = "nop"

    async def stream(  # pragma: no cover
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(  # pragma: no cover
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="", tool_calls=())

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── helper: stub the learned-skills loader for these tests ───────────


@dataclass
class _StubLoader:
    skills: list[LearnedSkill] = field(default_factory=list)

    def list_skills(self) -> list[LearnedSkill]:
        return self.skills


def _skill(skill_id: str, *, title: str = "", triggers: list[str] | None = None,
           body: str = "") -> LearnedSkill:
    return LearnedSkill(
        skill_id=skill_id,
        title=title or skill_id,
        description="",
        triggers=triggers or [],
        body=body,
        source_path=Path("/tmp/" + skill_id),
        mtime=0.0,
    )


@pytest.fixture
def patched_loader(monkeypatch: pytest.MonkeyPatch):
    stub = _StubLoader()

    def _factory():
        return stub

    monkeypatch.setattr(
        "xmclaw.daemon.learned_skills.default_learned_skills_loader",
        _factory,
    )
    return stub


# ── test fixtures ────────────────────────────────────────────────────


def _make_agent() -> AgentLoop:
    return AgentLoop(llm=_NopLLM(), bus=InProcessEventBus())


async def _capture(agent: AgentLoop, **kwargs: Any) -> list[tuple[Any, dict]]:
    """Drive _detect_skill_invocations with a recording publish callable."""
    captured: list[tuple[Any, dict]] = []

    async def _publish(event_type: Any, payload: dict) -> None:
        captured.append((event_type, payload))

    await agent._detect_skill_invocations(
        _publish,
        kwargs.get("session_id", "test-sess"),
        kwargs.get("user_message", ""),
        kwargs.get("assistant_text", ""),
        kwargs.get("tool_calls", []),
    )
    return captured


# ── B-122 cases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_word_boundary_skill_id_fires(patched_loader) -> None:
    """Skill 'git_helper' fires when the user message contains the
    exact token (word boundaries hold)."""
    patched_loader.skills = [_skill("git_helper")]
    agent = _make_agent()
    events = await _capture(
        agent,
        user_message="run git_helper for me",
        assistant_text="ok",
    )
    types = [e[0] for e in events]
    assert EventType.SKILL_INVOKED in types


@pytest.mark.asyncio
async def test_word_boundary_blocks_substring_false_positive(
    patched_loader,
) -> None:
    """Skill 'git_status' must NOT fire on 'git status' — that's a
    generic command phrase, not a skill invocation."""
    patched_loader.skills = [_skill("git_status")]
    agent = _make_agent()
    events = await _capture(
        agent,
        user_message="run git status please",
        assistant_text="here is the git status output",
    )
    types = [e[0] for e in events]
    assert EventType.SKILL_INVOKED not in types


@pytest.mark.asyncio
async def test_enumeration_context_suppresses_all_detection(
    patched_loader,
) -> None:
    """When 3+ distinct skill_ids appear in assistant_text, the agent
    is listing skills, not invoking them. No SKILL_INVOKED should fire
    even though every skill_id matches."""
    patched_loader.skills = [
        _skill("auto_repair_v1"),
        _skill("auto_summary_v3"),
        _skill("auto_grep_v2"),
        _skill("auto_format_v1"),
    ]
    agent = _make_agent()
    events = await _capture(
        agent,
        user_message="what skills do you have?",
        assistant_text=(
            "I have auto_repair_v1, auto_summary_v3, auto_grep_v2, "
            "and auto_format_v1 available."
        ),
    )
    types = [e[0] for e in events]
    assert EventType.SKILL_INVOKED not in types


@pytest.mark.asyncio
async def test_two_skill_ids_does_not_trigger_enumeration_guard(
    patched_loader,
) -> None:
    """Threshold is 3+. Two skills mentioned together is plausibly a
    real two-skill turn, so detection still runs."""
    patched_loader.skills = [
        _skill("auto_repair_v1"),
        _skill("auto_summary_v3"),
    ]
    agent = _make_agent()
    events = await _capture(
        agent,
        user_message="please use auto_repair_v1",
        assistant_text="running auto_repair_v1 then auto_summary_v3",
    )
    types = [e[0] for e in events]
    assert EventType.SKILL_INVOKED in types


@pytest.mark.asyncio
async def test_short_title_does_not_match(patched_loader) -> None:
    """Title of length 4 ('code') no longer matches by title rule —
    raised threshold to 6 to drop low-information matches. (skill_id
    rule still applies — but here skill_id is also too short, so
    nothing fires.)"""
    patched_loader.skills = [_skill("c", title="code", triggers=[], body="")]
    agent = _make_agent()
    events = await _capture(
        agent,
        user_message="please review my code",
        assistant_text="here's the code",
    )
    types = [e[0] for e in events]
    assert EventType.SKILL_INVOKED not in types


@pytest.mark.asyncio
async def test_long_title_still_matches(patched_loader) -> None:
    """Titles ≥ 6 chars still match by title rule (e.g. 'database
    migration helper')."""
    patched_loader.skills = [_skill(
        "x", title="database migration helper", triggers=[], body="",
    )]
    agent = _make_agent()
    events = await _capture(
        agent,
        user_message="run my database migration helper now",
        assistant_text="ok",
    )
    types = [e[0] for e in events]
    assert EventType.SKILL_INVOKED in types
