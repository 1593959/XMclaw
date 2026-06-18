"""End-to-end turn latency benchmarks.

Run with: pytest tests/perf/test_turn_latency.py -v

These tests use mocked LLM / tool / memory backends so they measure
*agent-loop overhead* rather than provider wall-clock.  The thresholds
are intentionally loose (2–5 s) so CI runners with noisy neighbours stay
green; the value is in tracking *relative* movement across commits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider


# ── scripted mock LLM with configurable latency ───────────────────────────


@dataclass
class MockLLM(LLMProvider):
    """Mock LLM with configurable per-hop latency.

    ``script`` works like ``_ScriptedLLM`` in the unit suite: the i-th
    call returns the i-th ``LLMResponse``.  ``latency_ms`` is injected
    as an ``asyncio.sleep`` inside ``complete_streaming`` so the
    benchmark captures realistic async scheduling overhead.
    """

    latency_ms: float = 100.0
    script: list[LLMResponse] = field(default_factory=list)
    model: str = "mock/test"
    _i: int = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        if self._i >= len(self.script):
            raise RuntimeError(
                f"MockLLM exhausted after {len(self.script)} calls"
            )
        resp = self.script[self._i]
        self._i += 1
        return resp

    async def complete_streaming(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        on_chunk: Any = None,
        on_thinking_chunk: Any = None,
        on_tool_block: Any = None,
        on_stream_fallback: Any = None,
        cancel: asyncio.Event | None = None, extended_thinking: Any = None, **_kw: Any,
    ) -> LLMResponse:
        # Inject wall-clock latency so the benchmark isn't measuring
        # only coroutine switch cost.
        await asyncio.sleep(self.latency_ms / 1000.0)
        resp = await self.complete(messages, tools=tools)
        # Always emit a chunk so the agent loop's first-token guard
        # (max(total/3, 5s)) doesn't add artificial wait for tool-only
        # responses where content is empty.
        if on_chunk is not None:
            await on_chunk(resp.content or " ")
        if on_tool_block is not None:
            for tc in resp.tool_calls or ():
                try:
                    on_tool_block(tc)
                except Exception:  # noqa: BLE001
                    pass
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── stub tool provider ─────────────────────────────────────────────────────


@dataclass
class _StubToolProvider(ToolProvider):
    specs: list[ToolSpec] = field(default_factory=list)
    results: dict[str, ToolResult] = field(default_factory=dict)
    invoke_latency_ms: float = 50.0

    def list_tools(self) -> list[ToolSpec]:
        return list(self.specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        if self.invoke_latency_ms:
            await asyncio.sleep(self.invoke_latency_ms / 1000.0)
        result = self.results.get(call.name)
        if result is None:
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"no stub for {call.name}",
            )
        return ToolResult(
            call_id=call.id,
            ok=result.ok,
            content=result.content,
            error=result.error,
            latency_ms=result.latency_ms,
            side_effects=result.side_effects,
        )


# ── stub memory service for recall latency ─────────────────────────────────


class _StubMemoryHit:
    """Minimal hit object consumed by ``recall_for_message``."""

    def __init__(self, fact: Any, distance: float) -> None:
        self.fact = fact
        self.distance = distance


class _StubMemoryFact:
    """Minimal fact object consumed by ``recall_for_message``."""

    def __init__(
        self,
        id: str,
        text: str,
        bucket: str,
        kind: str,
        ts_first: float,
    ) -> None:
        self.id = id
        self.text = text
        self.bucket = bucket
        self.kind = kind
        self.ts_first = ts_first


class _StubMemoryService:
    """Mock memory v2 service with configurable recall latency."""

    def __init__(self, recall_latency_ms: float = 100.0) -> None:
        self.recall_latency_ms = recall_latency_ms

    async def recall(
        self,
        query: Any,
        *,
        k: int = 8,
        min_confidence: float = 0.0,
        include_relations: bool = False,
        include_superseded: bool = False,
    ) -> list[_StubMemoryHit]:
        if self.recall_latency_ms:
            await asyncio.sleep(self.recall_latency_ms / 1000.0)
        # Return one synthetic hit so the agent loop actually prepends
        # the <recalled> block (exercises the full code path).
        return [
            _StubMemoryHit(
                fact=_StubMemoryFact(
                    id="fact-1",
                    text="User prefers dark mode",
                    bucket="misc",
                    kind="fact",
                    ts_first=0.0,
                ),
                distance=0.1,  # similarity = 0.9
            ),
        ]


# ── benchmarks ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_simple_dialogue_turn_latency() -> None:
    """Benchmark: simple dialogue turn should complete in < 2 s."""
    bus = InProcessEventBus()
    llm = MockLLM(
        latency_ms=500,
        script=[
            LLMResponse(
                content="Mock response",
                tool_calls=(),
                stop_reason="end_turn",
            ),
        ],
    )
    agent = AgentLoop(
        llm=llm,
        bus=bus,
        system_prompt="You are a test agent.",
    )

    t0 = time.perf_counter()
    result = await agent.run_turn("test-simple", "hello")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert result.ok
    assert result.text == "Mock response"
    assert elapsed_ms < 2000, (
        f"Simple turn took {elapsed_ms:.0f} ms, expected < 2000 ms"
    )


@pytest.mark.asyncio
async def test_tool_chain_turn_latency() -> None:
    """Benchmark: tool chain turn should complete in < 5 s.

    Two hops:
      1. LLM emits a tool call (500 ms) + tool invocation (50 ms)
      2. LLM emits final text (500 ms)
    Total expected ~1.1 s; threshold 5 s gives headroom for CI noise.
    """
    bus = InProcessEventBus()
    llm = MockLLM(
        latency_ms=500,
        script=[
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="echo",
                        args={"x": 1},
                        provenance="anthropic",
                        id="tc-1",
                    ),
                ),
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="Tool result processed",
                tool_calls=(),
                stop_reason="end_turn",
            ),
        ],
    )
    tools = _StubToolProvider(
        specs=[
            ToolSpec(
                name="echo",
                description="echoes",
                parameters_schema={"type": "object"},
            ),
        ],
        results={
            "echo": ToolResult(
                call_id="",
                ok=True,
                content={"echoed": 1},
                side_effects=(),
            ),
        },
        invoke_latency_ms=50,
    )
    agent = AgentLoop(
        llm=llm,
        bus=bus,
        tools=tools,
        system_prompt="You are a test agent with tools.",
    )

    t0 = time.perf_counter()
    result = await agent.run_turn("test-tool-chain", "please echo")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert result.ok
    assert result.text == "Tool result processed"
    assert result.hops == 2
    assert elapsed_ms < 5000, (
        f"Tool chain turn took {elapsed_ms:.0f} ms, expected < 5000 ms"
    )


@pytest.mark.asyncio
async def test_memory_recall_latency() -> None:
    """Benchmark: memory recall should complete in < 3 s.

    Auto-recall is enabled via config.  The mock memory service sleeps
    200 ms on recall; the LLM sleeps 500 ms.  Together they should stay
    well under the 3 s threshold, proving the recall path does not
    degenerate into the un-bounded scan seen in incident chat-b09a3ad4.
    """
    bus = InProcessEventBus()
    llm = MockLLM(
        latency_ms=500,
        script=[
            LLMResponse(
                content="I remember you prefer dark mode.",
                tool_calls=(),
                stop_reason="end_turn",
            ),
        ],
    )
    mem_svc = _StubMemoryService(recall_latency_ms=200)
    agent = AgentLoop(
        llm=llm,
        bus=bus,
        system_prompt="You are a test agent with memory.",
        memory_service=mem_svc,
        cfg={
            "cognition": {
                "auto_recall": {
                    "enabled": True,
                    "timeout_s": 5.0,
                    "k": 4,
                    "min_similarity": 0.5,
                },
            },
        },
    )

    t0 = time.perf_counter()
    result = await agent.run_turn("test-memory", "what do you know about me")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert result.ok
    assert "dark mode" in result.text
    assert elapsed_ms < 3000, (
        f"Memory recall turn took {elapsed_ms:.0f} ms, expected < 3000 ms"
    )
