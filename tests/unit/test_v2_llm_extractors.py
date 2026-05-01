"""LLM-backed extractors — unit tests (Epic #24 Phase 3.5).

Locks the contract:

* ``build_skill_extractor`` calls ``llm.complete`` with system+user
  prompt and parses the JSON-array response into ProposedSkill.
* ``build_profile_extractor`` does the same for ProfileDelta.
* Tolerant JSON parser handles raw JSON, fenced ```json blocks, and
  text-with-prose-around-the-array.
* Bad LLM returns (non-list, missing fields, invalid types) drop
  cleanly without raising.
* LLM exceptions are isolated (extractor returns []).
* Empty input → no LLM call (cheap fast-path).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from xmclaw.core.evolution import ProposedSkill
from xmclaw.core.evolution.proposer import _Pattern
from xmclaw.core.journal import JournalEntry, ToolCallSummary
from xmclaw.core.profile import ProfileDelta
from xmclaw.daemon.llm_extractors import (
    _parse_json_array,
    build_profile_extractor,
    build_skill_extractor,
)
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.core.ir import ToolCallShape, ToolSpec


# ── fake LLM ────────────────────────────────────────────────────────


@dataclass
class _ScriptedLLM(LLMProvider):
    """Returns the i-th scripted response on the i-th call.

    Shared with test_v2_agent_loop._ScriptedLLM in shape but kept
    separate to avoid a cross-test import dependency."""

    script: list[str] = field(default_factory=list)
    raise_on_call: bool = False
    captured: list[list[Message]] = field(default_factory=list)
    _i: int = 0
    model: str = "scripted"

    async def stream(  # pragma: no cover
        self, messages, tools=None, *, cancel=None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        self.captured.append(list(messages))
        if self.raise_on_call:
            raise RuntimeError("simulated LLM failure")
        if self._i >= len(self.script):
            raise RuntimeError("scripted LLM exhausted")
        text = self.script[self._i]
        self._i += 1
        return LLMResponse(content=text)

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── parser ──────────────────────────────────────────────────────────


def test_parse_raw_json_array() -> None:
    assert _parse_json_array('[{"a": 1}]') == [{"a": 1}]


def test_parse_fenced_json_array() -> None:
    text = "Some prose first.\n```json\n[{\"a\": 2}]\n```\nMore prose."
    assert _parse_json_array(text) == [{"a": 2}]


def test_parse_inline_array_with_prose() -> None:
    text = "Here you go: [{\"a\": 3}, {\"b\": 4}]. Hope it helps!"
    parsed = _parse_json_array(text)
    assert parsed == [{"a": 3}, {"b": 4}]


def test_parse_empty_string() -> None:
    assert _parse_json_array("") == []


def test_parse_garbage_returns_empty() -> None:
    assert _parse_json_array("not json at all") == []


def test_parse_object_not_array_returns_empty() -> None:
    """Top-level dict is not an array — must drop, not coerce."""
    assert _parse_json_array('{"a": 1}') == []


# ── skill extractor ─────────────────────────────────────────────────


def _pattern(name: str, sids: tuple[str, ...]) -> _Pattern:
    return _Pattern(
        tool_name=name, session_ids=sids,
        occurrence_count=len(sids), avg_grader_score=None,
    )


def _journal_entry(sid: str) -> JournalEntry:
    return JournalEntry(
        session_id=sid, agent_id="a",
        ts_start=0.0, ts_end=1.0, duration_s=1.0,
        turn_count=1,
        tool_calls=(ToolCallSummary(name="t", ok=True),),
    )


@pytest.mark.asyncio
async def test_skill_extractor_happy_path() -> None:
    llm = _ScriptedLLM(script=["""
[
  {
    "skill_id": "git.status",
    "title": "Git Status Workflow",
    "description": "Check git status before any code changes",
    "body": "step 1: bash git status\\nstep 2: review changes",
    "triggers": ["git", "status"],
    "confidence": 0.85,
    "evidence": ["sess-1", "sess-2"],
    "source_pattern": "tool 'bash' with git in 5 sessions"
  }
]
"""])
    extractor = build_skill_extractor(llm)
    patterns = [_pattern("bash", ("sess-1", "sess-2"))]
    entries = [_journal_entry("sess-1"), _journal_entry("sess-2")]
    result = await extractor(patterns, entries)

    assert len(result) == 1
    assert isinstance(result[0], ProposedSkill)
    assert result[0].skill_id == "git.status"
    assert result[0].confidence == 0.85
    assert result[0].evidence == ("sess-1", "sess-2")

    # System + user message both passed.
    assert len(llm.captured) == 1
    msgs = llm.captured[0]
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    assert "bash" in msgs[1].content
    assert "sess-1" in msgs[1].content


@pytest.mark.asyncio
async def test_skill_extractor_empty_patterns_skips_llm() -> None:
    """No patterns → no LLM call (cost-saving fast path)."""
    llm = _ScriptedLLM(script=["[{}]"])  # would crash if called
    extractor = build_skill_extractor(llm)
    result = await extractor([], [])
    assert result == []
    assert llm.captured == []


@pytest.mark.asyncio
async def test_skill_extractor_drops_invalid_entries() -> None:
    llm = _ScriptedLLM(script=["""
[
  {"skill_id": "good", "evidence": ["s1"], "confidence": 0.9},
  {"missing_skill_id": true},
  "not even a dict",
  {"skill_id": "no_evidence", "evidence": [], "confidence": 0.9}
]
"""])
    extractor = build_skill_extractor(llm)
    patterns = [_pattern("x", ("s1",))]
    result = await extractor(patterns, [])
    # Only the first ("good") survives. "no_evidence" fails the
    # ABI-level evidence-non-empty check inside ProposedSkill;
    # "missing_skill_id" lacks required field; string isn't a dict.
    assert len(result) == 1
    assert result[0].skill_id == "good"


@pytest.mark.asyncio
async def test_skill_extractor_llm_exception_isolated() -> None:
    llm = _ScriptedLLM(script=[], raise_on_call=True)
    extractor = build_skill_extractor(llm)
    patterns = [_pattern("x", ("s1",))]
    result = await extractor(patterns, [])
    assert result == []


@pytest.mark.asyncio
async def test_skill_extractor_handles_fenced_response() -> None:
    llm = _ScriptedLLM(script=[
        "Here are the proposals:\n```json\n["
        '{"skill_id": "fenced", "evidence": ["s1"], "confidence": 0.7}'
        "]\n```\nDone!",
    ])
    extractor = build_skill_extractor(llm)
    result = await extractor([_pattern("x", ("s1",))], [])
    assert len(result) == 1
    assert result[0].skill_id == "fenced"


# ── profile extractor ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_extractor_happy_path() -> None:
    llm = _ScriptedLLM(script=["""
[
  {"kind": "preference", "text": "User prefers terse markdown answers", "confidence": 0.9},
  {"kind": "constraint", "text": "Never run rm -rf without asking", "confidence": 0.95}
]
"""])
    extractor = build_profile_extractor(llm)
    messages = [
        {"role": "user", "content": "Give me a one-line answer please"},
        {"role": "assistant", "content": "Sure thing"},
    ]
    meta = {
        "session_id": "s1",
        "last_user_event_id": "e1",
        "agent_id": "agent",
    }
    result = await extractor(messages, meta)
    assert len(result) == 2
    assert all(isinstance(d, ProfileDelta) for d in result)
    assert result[0].kind == "preference"
    assert result[0].source_session_id == "s1"
    assert result[0].source_event_id == "e1"
    assert result[1].kind == "constraint"


@pytest.mark.asyncio
async def test_profile_extractor_empty_messages_skips_llm() -> None:
    llm = _ScriptedLLM(script=["[]"])
    extractor = build_profile_extractor(llm)
    result = await extractor([], {"session_id": "s1"})
    assert result == []
    assert llm.captured == []


@pytest.mark.asyncio
async def test_profile_extractor_drops_invalid_entries() -> None:
    llm = _ScriptedLLM(script=["""
[
  {"kind": "preference", "text": "valid", "confidence": 0.9},
  {"text": ""},
  "not a dict",
  {"kind": "habit", "text": "valid 2 with default kind", "confidence": 0.7}
]
"""])
    extractor = build_profile_extractor(llm)
    result = await extractor(
        [{"role": "user", "content": "hi"}],
        {"session_id": "s1", "last_user_event_id": "e1"},
    )
    # Only valid ones survive (empty text rejected; non-dict rejected).
    assert len(result) == 2
    assert result[0].text == "valid"
    assert result[1].text == "valid 2 with default kind"
    assert result[1].kind == "habit"


@pytest.mark.asyncio
async def test_profile_extractor_llm_exception_isolated() -> None:
    llm = _ScriptedLLM(script=[], raise_on_call=True)
    extractor = build_profile_extractor(llm)
    result = await extractor(
        [{"role": "user", "content": "x"}],
        {"session_id": "s1", "last_user_event_id": "e1"},
    )
    assert result == []
