"""MemoryExtractor unit tests — Phase B of "agent 自己用记忆" (2026-05-10).

Covers the heuristic gate + LLM extract + JSON parse path with a
faked LLM (no network). Each test pins ONE behaviour so a future
refactor that breaks the gate, the prompt shape, or the JSON shape
fails loudly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.memory.extractor import (
    ExtractedFact,
    MemoryExtractor,
    _detect_trigger,
)


# ── Heuristic gate ───────────────────────────────────────────────


def test_trigger_user_fact_chinese() -> None:
    # Assistant responses must be neutral — Wave 26 fix-4 added
    # ``assistant_remember`` which has higher precedence than user_fact
    # (the agent's commitment to remember is a stronger signal than
    # the user's self-statement). Tests of user_fact in isolation
    # therefore use neutral assistant replies.
    assert _detect_trigger("我叫张三", "好的") == "user_fact"
    assert _detect_trigger("我习惯用 vim", "明白") == "user_fact"
    assert _detect_trigger("我喜欢深色主题", "好的") == "user_fact"


def test_trigger_assistant_remember_chinese() -> None:
    """Wave 26 fix-4: the agent's "记住了 / 我记下了 / 已记录" claims are
    promises the user expects honoured. The trigger fires regardless
    of what the user said, so the LLM extractor distils the relevant
    fact from context and persists it. This was the #1 silent failure
    before Wave 26: agent claims memorisation, no actual write."""
    assert _detect_trigger("我叫张三", "哥，记住了！") == "assistant_remember"
    assert _detect_trigger("admin/admin888", "已记录账号信息") == "assistant_remember"
    assert _detect_trigger("anything", "ok，记下了") == "assistant_remember"
    assert _detect_trigger("anything", "I'll remember that.") == "assistant_remember"
    assert _detect_trigger("anything", "noted!") == "assistant_remember"


def test_trigger_user_fact_english() -> None:
    assert _detect_trigger("My name is Alice", "Hi Alice") == "user_fact"
    assert _detect_trigger("I prefer dark mode", "ok") == "user_fact"
    assert _detect_trigger("Call me Bob", "noted") == "user_fact"


def test_trigger_decision_in_assistant_text() -> None:
    """Decisions live in the assistant response (the agent committed
    to a path). User message can be neutral."""
    assert _detect_trigger(
        "用 SQLite 还是 Postgres",
        "我们就用 SQLite，足够本地用。",
    ) == "decision"
    assert _detect_trigger(
        "auth approach?",
        "Let's go with JWT — simpler, fits the stateless design.",
    ) == "decision"


def test_trigger_completion_in_assistant_text() -> None:
    assert _detect_trigger(
        "ship it",
        "已部署到生产环境，监控正常。",
    ) == "completion"
    assert _detect_trigger(
        "merge?",
        "PR merged — feature shipped.",
    ) == "completion"


def test_trigger_explicit_remember_beats_other_signals() -> None:
    """``记住`` is a direct instruction. Even if the message also
    matches user_fact, remember wins (higher precedence)."""
    assert _detect_trigger(
        "记住 我习惯用 vim",  # both 记住 + 我习惯
        "好",
    ) == "remember"
    assert _detect_trigger(
        "remember this",
        "noted",
    ) == "remember"


def test_no_trigger_on_routine_turns() -> None:
    """Most turns are routine — extractor MUST NOT fire (saves an
    LLM call). Returning None signals the gate held."""
    assert _detect_trigger("天气怎么样", "今天 22°C 晴") is None
    assert _detect_trigger("show me the diff", "diff --git ...") is None
    assert _detect_trigger("", "") is None


def test_trigger_does_not_misfire_on_substring() -> None:
    """``.git`` ≠ ``.github`` style precision: ensure simple
    substring matches don't trigger spuriously. ``hi ghemu`` should
    NOT match ``i prefer`` even though letters overlap."""
    assert _detect_trigger("hi ghemu", "ok") is None
    # Word-boundary `\b` guards against ``preference`` matching
    # ``i prefer`` mid-word.
    assert _detect_trigger("preference shaping is hard", "ok") is None


# ── LLM extract path ─────────────────────────────────────────────


@dataclass
class _FakeLLMResponse:
    content: str


@dataclass
class _ScriptedLLM:
    """Returns whatever JSON content the test scripts; records the
    last prompt so tests can assert what we asked the LLM."""

    response: str
    last_prompt: str = ""
    calls: int = 0

    async def complete(self, messages: list, tools: Any = None) -> Any:  # noqa: ARG002
        self.calls += 1
        self.last_prompt = messages[-1].content if messages else ""
        return _FakeLLMResponse(content=self.response)


@pytest.mark.asyncio
async def test_extract_returns_none_when_gate_blocks() -> None:
    """Heuristic-gate path: routine turn → extractor returns None
    WITHOUT calling the LLM. Pinning this prevents a future refactor
    that "always asks the LLM" from silently doubling per-turn cost."""
    llm = _ScriptedLLM(response='{"text":"junk","node_type":"event","layer":"long_term","reason":"x"}')
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="天气怎么样",
        assistant_response="今天 22°C",
    )
    assert fact is None
    assert llm.calls == 0, (
        "extractor called the LLM even though heuristic should have "
        "blocked — gate broken, costs doubled per turn"
    )


@pytest.mark.asyncio
async def test_extract_calls_llm_on_user_fact_trigger() -> None:
    """Trigger fires → LLM is called → JSON parsed → fact returned."""
    llm = _ScriptedLLM(response='{"text":"User prefers vim","node_type":"entity","layer":"long_term","reason":"explicit preference"}')
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="我习惯用 vim",
        assistant_response="好",
    )
    assert llm.calls == 1
    assert fact is not None
    assert isinstance(fact, ExtractedFact)
    assert fact.text == "User prefers vim"
    assert fact.node_type == "entity"
    assert fact.layer == "long_term"
    assert fact.reason == "explicit preference"


@pytest.mark.asyncio
async def test_extract_returns_none_on_llm_null() -> None:
    """LLM agreed nothing's worth keeping (returned ``null``) →
    extractor returns None. False-positive heuristic gracefully
    bails."""
    llm = _ScriptedLLM(response="null")
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="我叫 X",
        assistant_response="ok",
    )
    assert fact is None


@pytest.mark.asyncio
async def test_extract_strips_markdown_code_fence() -> None:
    """LLM wrapped JSON in ```json``` despite our instructions —
    extractor must still parse correctly."""
    llm = _ScriptedLLM(response='```json\n{"text":"X","node_type":"event","layer":"long_term","reason":"y"}\n```')
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="记住 X",
        assistant_response="ok",
    )
    assert fact is not None
    assert fact.text == "X"


@pytest.mark.asyncio
async def test_extract_returns_none_on_unparseable_json() -> None:
    """LLM returned malformed JSON → log + return None (don't crash
    the turn)."""
    llm = _ScriptedLLM(response="this is not json at all { ::: ")
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="我叫 X",
        assistant_response="ok",
    )
    assert fact is None


@pytest.mark.asyncio
async def test_extract_clamps_invalid_node_type_to_event() -> None:
    """LLM returned ``node_type="lyric"`` (not in enum) — extractor
    clamps to ``event`` rather than rejecting. Resilience > strictness
    here; the fact text is fine, the metadata is just imperfect."""
    llm = _ScriptedLLM(
        response='{"text":"User likes vim","node_type":"lyric","layer":"long_term","reason":"x"}'
    )
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="我习惯 vim",
        assistant_response="ok",
    )
    assert fact is not None
    assert fact.node_type == "event"


@pytest.mark.asyncio
async def test_extract_rejects_empty_text() -> None:
    """``text=""`` is junk → return None. Stored empties pollute
    recall."""
    llm = _ScriptedLLM(
        response='{"text":"","node_type":"event","layer":"long_term","reason":"x"}'
    )
    ex = MemoryExtractor(llm=llm)
    fact = await ex.extract(
        user_message="我叫 X",
        assistant_response="ok",
    )
    assert fact is None


@pytest.mark.asyncio
async def test_extract_handles_llm_exception_gracefully() -> None:
    """LLM raised → extractor logs + returns None. Never propagates."""

    class _BoomLLM:
        async def complete(self, messages: list, tools: Any = None):  # noqa: ARG002
            raise RuntimeError("provider blew up")

    ex = MemoryExtractor(llm=_BoomLLM())
    fact = await ex.extract(
        user_message="我习惯 vim",
        assistant_response="ok",
    )
    assert fact is None


@pytest.mark.asyncio
async def test_extract_prompt_carries_trigger_reason() -> None:
    """The prompt must include the trigger-reason so the LLM knows
    WHY the heuristic flagged this turn — improves extract quality
    (the LLM can reject the heuristic's hint by returning null)."""
    llm = _ScriptedLLM(response="null")
    ex = MemoryExtractor(llm=llm)
    await ex.extract(
        user_message="我习惯用 vim",
        assistant_response="ok",
    )
    assert "用户陈述了关于自己的事实" in llm.last_prompt or "trigger" in llm.last_prompt.lower()
