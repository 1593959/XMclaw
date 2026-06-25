from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from xmclaw.context.summarizer_agent import SummarizerAgent


class _FakeLLM:
    def __init__(self, *, raises: Exception | None = None, content: str = "summary") -> None:
        self.raises = raises
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_summarizer_agent_wraps_llm_with_system_and_user_prompt() -> None:
    llm = _FakeLLM(content=" useful summary ")
    agent = SummarizerAgent(llm, timeout_s=5)

    result = await agent.summarize("summarize this", 123)

    assert result == "useful summary"
    assert len(llm.calls) == 1
    messages = llm.calls[0]["messages"]
    assert [m.role for m in messages] == ["system", "user"]
    assert "summarizer agent" in messages[0].content
    assert messages[1].content == "summarize this"
    assert llm.calls[0]["tools"] is None


@pytest.mark.asyncio
async def test_summarizer_agent_returns_none_on_missing_or_failed_llm() -> None:
    assert await SummarizerAgent(object()).summarize("x", 1) is None

    failing = SummarizerAgent(_FakeLLM(raises=RuntimeError("down")))
    assert await failing.summarize("x", 1) is None

    empty = SummarizerAgent(_FakeLLM(content=""))
    assert await empty.summarize("x", 1) is None
