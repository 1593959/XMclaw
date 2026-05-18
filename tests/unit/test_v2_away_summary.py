"""AwaySummary — Wave-32+ (2026-05-18).

Pure-function tests for :func:`xmclaw.cognition.away_summary.
generate_away_summary`. Stubs the LLM so no network call; covers:

* empty history → None
* normal history → calls LLM with truncated tail + prompt → returns text
* timeout → None
* LLM raises → None
* system messages excluded from the tail
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from xmclaw.cognition.away_summary import generate_away_summary
from xmclaw.core.ir import Message


@dataclass
class _StubResp:
    content: str
    tool_calls: tuple = ()


class _RecorderLLM:
    """LLMProvider stub that records the last messages it received and
    returns ``reply_text`` (or raises ``raise_with`` if set)."""

    def __init__(self, reply_text: str = "test recap", raise_with: Exception | None = None,
                 delay_s: float = 0.0) -> None:
        self.reply_text = reply_text
        self.raise_with = raise_with
        self.delay_s = delay_s
        self.last_messages: list[Message] = []
        self.call_count = 0

    async def complete(self, messages, tools=None):
        self.call_count += 1
        self.last_messages = list(messages)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raise_with is not None:
            raise self.raise_with
        return _StubResp(content=self.reply_text)

    def stream(self, messages, tools=None, *, cancel=None):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_empty_history_returns_none() -> None:
    llm = _RecorderLLM()
    out = await generate_away_summary([], llm)
    assert out is None
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_only_system_messages_returns_none() -> None:
    """System prompt-only history isn't meaningful — recap should
    surface None so the UI doesn't show an empty card."""
    llm = _RecorderLLM()
    out = await generate_away_summary(
        [Message(role="system", content="you are a helpful agent")],
        llm,
    )
    assert out is None
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_happy_path_returns_recap_text() -> None:
    llm = _RecorderLLM(reply_text="Building auth flow. Next: write tests.")
    history = [
        Message(role="system", content="system"),
        Message(role="user", content="add login"),
        Message(role="assistant", content="will do"),
    ]
    out = await generate_away_summary(history, llm)
    assert out == "Building auth flow. Next: write tests."
    # System message was excluded from what was sent to the LLM.
    sent_roles = [m.role for m in llm.last_messages]
    assert "system" not in sent_roles
    # The synthetic prompt is the last user message.
    assert llm.last_messages[-1].role == "user"
    assert "stepped away" in llm.last_messages[-1].content


@pytest.mark.asyncio
async def test_truncates_to_max_messages() -> None:
    """Long histories must be capped — the small-fast model can't
    take an unbounded window."""
    llm = _RecorderLLM(reply_text="ok")
    history = [
        Message(role="user", content=f"msg {i}") for i in range(50)
    ]
    await generate_away_summary(history, llm, max_messages=10)
    # 10 tail messages + 1 synthetic prompt = 11
    assert len(llm.last_messages) == 11
    # First retained tail message should be msg 40 (50 - 10).
    assert "msg 40" in llm.last_messages[0].content


@pytest.mark.asyncio
async def test_llm_error_returns_none() -> None:
    llm = _RecorderLLM(raise_with=RuntimeError("kaboom"))
    history = [Message(role="user", content="hi")]
    out = await generate_away_summary(history, llm)
    assert out is None


@pytest.mark.asyncio
async def test_timeout_returns_none() -> None:
    """A stuck recap must NOT block the user's next interaction —
    enforce the wait_for timeout returns None cleanly."""
    llm = _RecorderLLM(reply_text="too slow", delay_s=0.5)
    history = [Message(role="user", content="hi")]
    out = await generate_away_summary(history, llm, timeout_s=0.05)
    assert out is None


@pytest.mark.asyncio
async def test_empty_llm_reply_returns_none() -> None:
    """A whitespace-only reply isn't useful — collapse to None so
    the UI hides the card rather than showing blank."""
    llm = _RecorderLLM(reply_text="   ")
    history = [Message(role="user", content="hi")]
    out = await generate_away_summary(history, llm)
    assert out is None


@pytest.mark.asyncio
async def test_zero_max_messages_returns_none() -> None:
    """Edge case — caller passes 0 or negative window."""
    llm = _RecorderLLM()
    history = [Message(role="user", content="hi")]
    assert await generate_away_summary(history, llm, max_messages=0) is None
    assert await generate_away_summary(history, llm, max_messages=-5) is None
