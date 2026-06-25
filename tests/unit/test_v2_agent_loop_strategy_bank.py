"""Sprint 3 #6 AgentLoop integration — strategy bank retrieve + inject.

The bank itself ships in ``xmclaw/core/journal/strategy_bank.py``
(Sprint 3 #6 base commit `d7fe86d`). This file pins the integration
point: when a bank is wired AND the user message is non-empty AND
``retrieve`` returns ≥1 strategy, the agent_loop must inject a
``<curriculum-strategies>`` block into the user-content concat — the
LLM still decides whether to apply (Iron Rule #2: gate is the LLM,
never auto-mutate). The block must be stripped from history before
persistence so the bank-retrieved strategies never feed back into
subsequent retrieves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMResponse, Message


# ── tiny scaffolding — copied from the existing test_v2_agent_loop ──


@dataclass
class _ScriptedLLM:
    """Captures the messages it was called with so tests can assert on
    the content of the system prompt + user turn the agent assembled.
    """
    script: list[LLMResponse]
    captured_messages: list[list[Message]] | None = None
    model: str = "stub"

    def __post_init__(self) -> None:
        self.captured_messages = []

    async def complete_streaming(
        self, messages: list[Message], *, tools: Any = None,
        on_chunk: Any = None, on_thinking_chunk: Any = None,
        on_tool_block: Any = None, on_stream_fallback: Any = None,
        cancel: Any = None, extended_thinking: Any = None, **_kw: Any,
    ) -> LLMResponse:
        assert self.captured_messages is not None
        self.captured_messages.append(list(messages))
        if not self.script:
            return LLMResponse(content="", tool_calls=())
        return self.script.pop(0)

    async def complete(self, messages: list[Message], **_kw: Any) -> LLMResponse:
        return await self.complete_streaming(messages)


@dataclass(frozen=True)
class _FakeStrategy:
    """Duck-type a Strategy without depending on the real dataclass."""
    when_pattern: str
    then_action: str
    evidence_count: int
    confidence: float


class _RetrievingBank:
    """Returns a fixed set of strategies on every retrieve."""

    def __init__(self, strategies: list[_FakeStrategy]) -> None:
        self._strategies = strategies
        self.calls: list[tuple[str, int]] = []

    async def retrieve(
        self, query: str, limit: int = 3,
    ) -> list[_FakeStrategy]:
        self.calls.append((query, limit))
        return list(self._strategies[:limit])


class _RaisingBank:
    """Models a bank whose retrieve fails (network / sqlite-vec
    error). agent_loop must swallow + log without breaking the turn.
    """
    async def retrieve(self, query: str, limit: int = 3) -> list[Any]:
        raise RuntimeError("simulated bank retrieve failure")


def _last_user_msg(messages: list[Message]) -> str:
    """Return the last user-role message content as a string."""
    for m in reversed(messages):
        if m.role == "user":
            return m.content if isinstance(m.content, str) else ""
    return ""


def _llm_context(messages: list[Message]) -> str:
    return "\n\n".join(
        m.content for m in messages if isinstance(m.content, str)
    )


# ── tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b_strategy_bank_none_no_block_injected() -> None:
    """When no bank is wired (default), behaviour is unchanged — the
    user content has no ``<curriculum-strategies>`` block."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="ok", tool_calls=()),
    ])
    loop = AgentLoop(llm=llm, bus=bus)  # strategy_bank defaults to None
    await loop.run_turn("sess-no-bank", "hello world")

    assert llm.captured_messages
    user_text = _last_user_msg(llm.captured_messages[0])
    assert "hello world" in user_text
    assert "<curriculum-strategies>" not in user_text


@pytest.mark.asyncio
async def test_b_strategy_bank_hit_injects_block() -> None:
    """When the bank returns strategies, the user content gets a
    ``<curriculum-strategies>`` block listing them with conf + evidence."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="answer", tool_calls=()),
    ])
    bank = _RetrievingBank([
        _FakeStrategy(
            when_pattern="user asks for code refactoring",
            then_action="batch reads then plan before edits",
            evidence_count=4,
            confidence=0.55,
        ),
        _FakeStrategy(
            when_pattern="long file > 1000 lines",
            then_action="grep first to locate change site",
            evidence_count=3,
            confidence=0.45,
        ),
    ])
    loop = AgentLoop(llm=llm, bus=bus, strategy_bank=bank)
    await loop.run_turn("sess-with-bank", "refactor this file")

    llm_context = _llm_context(llm.captured_messages[0])
    assert "<curriculum-strategies>" in llm_context
    assert "</curriculum-strategies>" in llm_context
    assert "user asks for code refactoring" in llm_context
    assert "batch reads then plan" in llm_context
    assert "evidence: 4 traces" in llm_context
    assert "conf 0.55" in llm_context
    # Bank.retrieve was called with the user message + strategy_top_k=3.
    assert bank.calls == [("refactor this file", 3)]


@pytest.mark.asyncio
async def test_b_strategy_bank_empty_hit_no_block() -> None:
    """Bank returns 0 strategies → no block (don't pollute prompt
    with empty headers)."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="answer", tool_calls=()),
    ])
    bank = _RetrievingBank([])  # empty
    loop = AgentLoop(llm=llm, bus=bus, strategy_bank=bank)
    await loop.run_turn("sess-empty-bank", "hello")

    user_text = _last_user_msg(llm.captured_messages[0])
    assert "<curriculum-strategies>" not in user_text


@pytest.mark.asyncio
async def test_b_strategy_bank_failure_swallowed() -> None:
    """Bank raises during retrieve → agent_loop logs + continues. The
    turn must still complete without ``<curriculum-strategies>`` block."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="answer", tool_calls=()),
    ])
    loop = AgentLoop(llm=llm, bus=bus, strategy_bank=_RaisingBank())
    res = await loop.run_turn("sess-broken-bank", "hello")
    # Turn completes successfully (no propagated exception).
    assert res.text == "answer"
    user_text = _last_user_msg(llm.captured_messages[0])
    assert "<curriculum-strategies>" not in user_text


@pytest.mark.asyncio
async def test_b_strategy_block_stripped_from_history() -> None:
    """After the turn, the persisted history must NOT contain the
    ``<curriculum-strategies>`` block — otherwise the next turn would
    re-feed bank-retrieved strategies back into the LLM (and the next
    distill pass would see them as user content)."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="answer", tool_calls=()),
        LLMResponse(content="answer2", tool_calls=()),
    ])
    bank = _RetrievingBank([
        _FakeStrategy(
            when_pattern="X",
            then_action="Y",
            evidence_count=2,
            confidence=0.50,
        ),
    ])
    loop = AgentLoop(llm=llm, bus=bus, strategy_bank=bank)
    await loop.run_turn("sess-strip", "first turn")
    # Second turn — the FIRST turn's user content as it appears in
    # history (passed to LLM in the second call) must NOT carry the
    # injected block.
    await loop.run_turn("sess-strip", "second turn")

    second_call_messages = llm.captured_messages[1]
    history_user_msgs = [
        m.content for m in second_call_messages
        if m.role == "user" and "first turn" in (m.content or "")
    ]
    assert history_user_msgs, "first-turn user message missing from history"
    for content in history_user_msgs:
        assert "<curriculum-strategies>" not in content, (
            "strategy block leaked into history — would re-feed itself"
        )


@pytest.mark.asyncio
async def test_b_strategy_top_k_param_respected() -> None:
    """``strategy_top_k`` constructor arg controls the bank's limit."""
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(content="answer", tool_calls=()),
    ])
    bank = _RetrievingBank([
        _FakeStrategy("a", "A", 2, 0.5),
        _FakeStrategy("b", "B", 2, 0.5),
    ])
    loop = AgentLoop(llm=llm, bus=bus, strategy_bank=bank, strategy_top_k=5)
    await loop.run_turn("sess-topk", "go")
    assert bank.calls[0][1] == 5
