"""B-125 — LearnedSkillToolProvider unit tests.

Pins:
  * each LearnedSkill becomes a ``learned_skill_<id>`` tool with
    description containing title + triggers + description
  * invoke() returns the full SKILL.md body verbatim as content
  * unknown tool name returns structured error (no exception)
  * publishes a deterministic SKILL_INVOKED event when invoked
    (evidence='tool_call' — distinct from B-122 heuristic path)
  * tool list rebuilds on each list_tools() (new SKILL.md visible
    without daemon restart)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import EventType, InProcessEventBus
from xmclaw.core.ir import ToolCall
from xmclaw.daemon.learned_skills import LearnedSkill
from xmclaw.daemon.learned_skills_tool import (
    LearnedSkillToolProvider,
    _to_tool_name,
)


# ── helpers ────────────────────────────────────────────────────────


def _learned(skill_id: str, *, body: str = "step 1\nstep 2",
             title: str = "", triggers: list[str] | None = None,
             description: str = "") -> LearnedSkill:
    return LearnedSkill(
        skill_id=skill_id,
        title=title or skill_id,
        description=description,
        triggers=triggers or [],
        body=body,
        source_path=Path("/tmp/" + skill_id),
        mtime=0.0,
    )


@dataclass
class _StubLoader:
    skills: list[LearnedSkill] = field(default_factory=list)

    def list_skills(self) -> list[LearnedSkill]:
        return list(self.skills)


async def _collect(bus: InProcessEventBus) -> list[Any]:
    captured: list[Any] = []

    async def _h(e: Any) -> None:
        captured.append(e)

    bus.subscribe(lambda e: True, _h)
    return captured


# ── name encoding ─────────────────────────────────────────────────


def test_tool_name_prefix_and_dot_mapping() -> None:
    assert _to_tool_name("auto.repair") == "learned_skill_auto__repair"
    assert _to_tool_name("plain_id") == "learned_skill_plain_id"


# ── list_tools ────────────────────────────────────────────────────


def test_list_tools_one_per_skill() -> None:
    loader = _StubLoader([
        _learned("auto_repair_v1", description="Fix broken builds"),
        _learned("auto_summary_v3"),
    ])
    provider = LearnedSkillToolProvider(loader)
    names = sorted(s.name for s in provider.list_tools())
    assert names == ["learned_skill_auto_repair_v1",
                     "learned_skill_auto_summary_v3"]


def test_list_tools_description_contains_title_and_triggers() -> None:
    loader = _StubLoader([_learned(
        "auto_repair_v1",
        title="Build Fixer",
        description="Fixes broken builds by parsing errors",
        triggers=["build_failed", "ci_red"],
    )])
    spec = LearnedSkillToolProvider(loader).list_tools()[0]
    assert "Build Fixer" in spec.description
    assert "Fixes broken builds" in spec.description
    assert "build_failed" in spec.description


def test_list_tools_dynamic_after_new_skill() -> None:
    loader = _StubLoader([])
    provider = LearnedSkillToolProvider(loader)
    assert provider.list_tools() == []
    loader.skills.append(_learned("late"))
    assert [s.name for s in provider.list_tools()] == ["learned_skill_late"]


def test_list_tools_max_cap() -> None:
    loader = _StubLoader([_learned(f"s{i}") for i in range(50)])
    provider = LearnedSkillToolProvider(loader, max_tools=10)
    assert len(provider.list_tools()) == 10


# ── invoke ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_returns_full_body() -> None:
    body = "# Procedure\n\n1. read the file\n2. fix it\n3. test"
    loader = _StubLoader([_learned("auto_x", body=body)])
    provider = LearnedSkillToolProvider(loader)
    result = await provider.invoke(ToolCall(
        name="learned_skill_auto_x", args={}, provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == body


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_error() -> None:
    provider = LearnedSkillToolProvider(_StubLoader([]))
    result = await provider.invoke(ToolCall(
        name="learned_skill_does_not_exist",
        args={}, provenance="synthetic",
    ))
    assert result.ok is False
    assert "unknown" in (result.error or "")


@pytest.mark.asyncio
async def test_invoke_caps_pathologically_large_body() -> None:
    """8 KB cap is belt-and-suspenders — a 50 KB SKILL.md body should
    not blow up the next LLM call."""
    big = "x" * 50_000
    loader = _StubLoader([_learned("big", body=big)])
    provider = LearnedSkillToolProvider(loader)
    result = await provider.invoke(ToolCall(
        name="learned_skill_big", args={}, provenance="synthetic",
    ))
    assert result.ok is True
    assert len(result.content) == 8192


@pytest.mark.asyncio
async def test_invoke_publishes_skill_invoked_event() -> None:
    """Deterministic invocation tracking — replaces B-122 heuristic
    path for tool-call invocations. evidence='tool_call' so the
    Evolution UI can distinguish."""
    loader = _StubLoader([_learned("auto_x", body="proc")])
    bus = InProcessEventBus()
    captured = await _collect(bus)
    provider = LearnedSkillToolProvider(loader, bus=bus, agent_id="ag-1")
    await provider.invoke(ToolCall(
        name="learned_skill_auto_x", args={},
        provenance="synthetic", session_id="sess-7",
    ))
    await bus.drain()

    invoked = [e for e in captured if e.type == EventType.SKILL_INVOKED]
    assert len(invoked) == 1
    payload = invoked[0].payload
    assert payload["skill_id"] == "auto_x"
    assert payload["evidence"] == "tool_call"
    assert invoked[0].session_id == "sess-7"
    assert invoked[0].agent_id == "ag-1"


@pytest.mark.asyncio
async def test_invoke_works_without_bus() -> None:
    """bus is optional — provider must not crash when not wired."""
    loader = _StubLoader([_learned("x", body="proc")])
    provider = LearnedSkillToolProvider(loader)  # no bus
    result = await provider.invoke(ToolCall(
        name="learned_skill_x", args={}, provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == "proc"
