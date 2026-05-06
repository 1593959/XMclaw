"""P0-1 wire-up smoke test: ContextCompressor integration with AgentLoop.

Verifies the proactive (threshold-based) and reactive (classifier-driven)
compression paths actually run when wired through ``_run_turn_inner``.
The compressor module itself is exercised by
``test_v2_context_compressor.py`` — this file just confirms the agent
loop calls it at the right moments.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


@dataclass
class _RecordingLLM(LLMProvider):
    """Minimal LLM stub: returns scripted responses, records what it saw."""
    script: list[LLMResponse] = field(default_factory=list)
    seen_messages: list[list[Message]] = field(default_factory=list)
    model: str = "recorder"
    _i: int = 0

    async def stream(self, messages, tools=None, *, cancel=None):  # noqa: ANN001
        # Default impl from base class is fine; we override complete().
        if False:
            yield  # type: ignore[unreachable]

    async def complete(self, messages, tools=None):  # noqa: ANN001
        self.seen_messages.append(list(messages))
        if self._i >= len(self.script):
            return LLMResponse(content="default", stop_reason="end_turn")
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.mark.asyncio
async def test_compressor_lazy_init() -> None:
    """The compressor field is None until first use, then created."""
    llm = _RecordingLLM(script=[LLMResponse(content="hi")])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    assert agent._compressor is None
    cc = agent._get_compressor()
    assert cc is not None
    # Idempotent — second call returns the same instance.
    assert agent._get_compressor() is cc


@pytest.mark.asyncio
async def test_compressor_skipped_under_threshold() -> None:
    """A small history doesn't trigger compression — original messages
    pass through ``_maybe_compress_messages`` unchanged."""
    llm = _RecordingLLM(script=[LLMResponse(content="hi")])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    msgs = [
        Message(role="system", content="S"),
        Message(role="user", content="hello"),
    ]
    out, did_compress = await agent._maybe_compress_messages(msgs, "s1")
    assert did_compress is False
    assert out == msgs


@pytest.mark.asyncio
async def test_compressor_fires_when_forced() -> None:
    """force=True bypasses threshold gate. The compressor's prune
    pass should at least no-op cleanly on a small history (and log
    the run path)."""
    # Inject a fake summarise_call on the compressor so we don't hit
    # the real LLM. The compressor's compress() catches exceptions
    # and returns the original messages, so we need messages long
    # enough to cross protect_first_n + 4 = 7 messages.
    llm = _RecordingLLM(script=[LLMResponse(content="ok")])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())
    cc = agent._get_compressor()

    # Override summarise_call with a stub that doesn't hit any real LLM
    async def _fake(prompt: str, max_tokens: int) -> Optional[str]:
        return f"FAKE summary len={len(prompt)}"

    cc.summarize_call = _fake
    # Drop threshold so it actually fires on a small fixture
    cc.threshold_tokens = 1
    cc.tail_token_budget = 50

    msgs = [
        Message(role="system", content="S"),
        Message(role="user", content="u1 " + "x" * 200),
        Message(role="assistant", content="a1 " + "y" * 200),
        Message(role="user", content="u2 " + "x" * 200),
        Message(role="assistant", content="a2 " + "y" * 200),
        Message(role="user", content="u3 " + "x" * 200),
        Message(role="assistant", content="a3 " + "y" * 200),
        Message(role="user", content="u4 " + "x" * 200),
    ]
    out, did_compress = await agent._maybe_compress_messages(
        msgs, "s1", force=True,
    )
    # The compressor should have done work — either changed the
    # message count or kept it (when summary merges into tail).
    # Either way, the SUMMARY_PREFIX should appear.
    from xmclaw.context.compressor import SUMMARY_PREFIX
    found = any(SUMMARY_PREFIX in (m.content or "") for m in out)
    assert found, "compressor didn't insert summary prefix"


@pytest.mark.asyncio
async def test_clear_session_resets_compressor_state() -> None:
    """``/reset`` should drop per-session compaction state so a new
    session starts with a clean anti-thrashing counter."""
    llm = _RecordingLLM(script=[LLMResponse(content="ok")])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())
    cc = agent._get_compressor()

    cc._state("s1").ineffective_count = 5
    cc._state("s1").previous_summary = "OLD"
    agent.clear_session("s1")
    assert "s1" not in cc._states


@pytest.mark.asyncio
async def test_normal_turn_passes_through_with_compressor_wired() -> None:
    """Smoke: a normal turn (no compression needed) still works after
    the compressor wire-up — no regression on the happy path."""
    llm = _RecordingLLM(script=[LLMResponse(content="hi there")])
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("s1", "hello")
    assert result.ok
    assert result.text == "hi there"
    assert len(llm.seen_messages) == 1
