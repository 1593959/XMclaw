"""Empty-response nudge (2026-06-15).

When the model returns empty text after executing tool calls, the hop
loop must append a system nudge and give the model one more hop instead
of silently returning an empty answer.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolResult, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMChunk, LLMProvider, LLMResponse, Message, Pricing
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.security.undo_cabinet import UndoCabinet


@dataclass
class _ScriptedLLM(LLMProvider):
    script: list[LLMResponse] = field(default_factory=list)
    _i: int = 0
    seen_messages: list[list[Message]] = field(default_factory=list)

    async def stream(  # pragma: no cover
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel=None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        self.seen_messages.append(list(messages))
        if self._i >= len(self.script):
            raise RuntimeError(f"_ScriptedLLM exhausted after {len(self.script)} calls")
        resp = self.script[self._i]
        self._i += 1
        return resp

    async def complete_streaming(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        on_chunk=None,
        on_thinking_chunk=None,
        on_tool_block=None,
        on_stream_fallback=None,
        cancel=None,
        extended_thinking=None,
    ):
        # Simulate a streaming provider that always emits at least an
        # empty chunk so the first-token guard doesn't hang on tool-only
        # responses in tests.
        response = await self.complete(messages, tools=tools)
        if on_chunk is not None:
            await on_chunk(response.content or "")
        if on_tool_block is not None:
            for tc in response.tool_calls or ():
                try:
                    on_tool_block(tc)
                except Exception:  # noqa: BLE001
                    pass
        return response

    @property
    def tool_call_shape(self):
        from xmclaw.core.ir import ToolCallShape
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@dataclass
class _EchoToolProvider(ToolProvider):
    def list_tools(self):
        return [
            ToolSpec(
                name="echo",
                description="echo",
                parameters_schema={"type": "object", "properties": {}},
            ),
        ]

    async def invoke(self, call):
        return ToolResult(call_id=call.id, ok=True, content="echo-result")


def _agent(tmp: Path, llm: LLMProvider, tools: ToolProvider | None = None) -> AgentLoop:
    bus = InProcessEventBus()
    cab = UndoCabinet(root=tmp / "undo")
    if tools is None:
        tools = BuiltinTools(allowed_dirs=[tmp], undo_cabinet=cab)
    agent = AgentLoop(llm=llm, bus=bus, tools=tools)
    agent._undo_cabinet = cab  # type: ignore[attr-defined]
    return agent


@pytest.mark.asyncio
async def test_empty_response_after_tools_gets_nudged(tmp_path: Path) -> None:
    """Model returns empty after a tool call → nudge → second chance."""
    from xmclaw.core.ir import ToolCall
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(id="call-1", name="echo", args={}, provenance="llm"),
            ),
        ),
        LLMResponse(content="", tool_calls=()),
        LLMResponse(content="summary", tool_calls=()),
    ])
    agent = _agent(tmp_path, llm, tools=_EchoToolProvider())

    result = await agent.run_turn("s1", "do it")
    assert result.text == "summary"
    # The nudge is turn-local context. It should reach the LLM without
    # becoming durable user history.
    seen_context = "\n\n".join(
        m.content or ""
        for batch in llm.seen_messages
        for m in batch
        if isinstance(m.content, str)
    )
    assert "你刚刚执行了工具调用" in seen_context
    history = agent._histories.get("s1", [])
    assert any(
        m.role == "assistant" and "summary" in (m.content or "")
        for m in history
    )


@pytest.mark.asyncio
async def test_nonempty_response_after_tools_skips_nudge(tmp_path: Path) -> None:
    """Model already gives text after tools → no nudge needed."""
    from xmclaw.core.ir import ToolCall
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(id="call-1", name="echo", args={}, provenance="llm"),
            ),
        ),
        LLMResponse(content="already done", tool_calls=()),
    ])
    agent = _agent(tmp_path, llm, tools=_EchoToolProvider())

    result = await agent.run_turn("s1", "do it")
    assert result.text == "already done"
    history = agent._histories.get("s1", [])
    assert not any(
        m.role == "user" and "你刚刚执行了工具调用" in (m.content or "")
        for m in history
    )
