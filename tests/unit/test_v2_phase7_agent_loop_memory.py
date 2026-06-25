"""AgentLoop ↔ MemoryService integration (V2, Phase 7.A.6+).

Replaces ``test_v2_agent_loop_unified_memory.py`` (V1 path retired in
Phase 7.A.6). Pins both directions end-to-end against an in-memory
V2 MemoryService:

  Read path  (run_turn):  recall the user message, inject hits into
                          the LLM prompt as ``<memory-recall>`` block,
                          publish MEMORY_RECALL.
  Write path (hop_loop):  background extract candidates via
                          ``LLMFactExtractor.extract_candidates``,
                          remember each via ``MemoryService.remember``,
                          publish one MEMORY_PUT_AUTO per candidate.

Coverage tradeoffs: this file uses InMemory backends + StubEmbedder
for determinism and does NOT exercise LanceDB. The LanceDB integration
is covered in ``test_v2_memory_v2_backend_lancedb.py``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.memory.v2 import (
    EmbeddingService,
    FactKind,
    FactScope,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    LLMFactExtractor,
    MemoryService,
    StubEmbedder,
)
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


# ── Fakes ────────────────────────────────────────────────────────


@dataclass
class _ScriptedLLM(LLMProvider):
    """Returns canned responses + records seen messages so tests can
    inspect whether the user content carried the ``<memory-recall>``
    block."""

    script: list[LLMResponse] = field(default_factory=list)
    seen_messages: list[list[Message]] = field(default_factory=list)
    model: str = "scripted"
    _i: int = 0

    async def stream(  # pragma: no cover
        self, messages: list[Message], tools: Any = None,
        *, cancel: Any = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self, messages: list[Message], tools: Any = None,
    ) -> LLMResponse:
        self.seen_messages.append(list(messages))
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


class _CapturingBus(InProcessEventBus):
    """Records every published event for post-turn assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.captured: list[Any] = []

    async def publish(self, event):  # noqa: ANN001
        self.captured.append(event)
        return await super().publish(event)


def _make_svc() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ── Read path: recall block injection ────────────────────────────


@pytest.mark.asyncio
async def test_recall_block_injected_into_user_message() -> None:
    """When MemoryService has facts, run_turn must include a
    ``<memory-recall>`` block in the user content the LLM sees."""
    svc = _make_svc()
    await svc.remember(
        "用户喜欢 Python 简洁",
        kind=FactKind.PREFERENCE,
        scope=FactScope.USER,
    )
    llm = _ScriptedLLM(script=[LLMResponse(content="OK")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=llm, bus=bus, memory_service=svc)
    await agent.run_turn(
        session_id="s1",
        user_message="什么是 Python 用户的偏好？",
    )
    # Inspect what the LLM saw — first complete() call.
    seen = llm.seen_messages[0]
    content = "\n\n".join(
        getattr(m, "content", "") or ""
        for m in seen
        if isinstance(getattr(m, "content", ""), str)
    )
    assert "<memory-recall>" in content
    assert "</memory-recall>" in content
    assert "Python" in content


@pytest.mark.asyncio
async def test_recall_block_omitted_when_no_hits() -> None:
    """Empty store → no recall block, but MEMORY_RECALL event still
    fires (so the UI activity timeline knows the agent queried)."""
    svc = _make_svc()
    llm = _ScriptedLLM(script=[LLMResponse(content="OK")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=llm, bus=bus, memory_service=svc)
    await agent.run_turn(session_id="s1", user_message="anything")
    seen = llm.seen_messages[0]
    user_msg = next(m for m in seen if getattr(m, "role", "") == "user")
    assert "<memory-recall>" not in (getattr(user_msg, "content", "") or "")
    recall_events = [
        e for e in bus.captured
        if getattr(e, "type", None) == EventType.MEMORY_RECALL
    ]
    assert len(recall_events) == 1
    assert recall_events[0].payload["hits"] == []


@pytest.mark.asyncio
async def test_recall_failure_does_not_break_turn() -> None:
    """Recall is best-effort: a broken service must not crash run_turn."""
    class _BrokenSvc:
        async def recall(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("vector backend offline")

    llm = _ScriptedLLM(script=[LLMResponse(content="OK")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=llm, bus=bus, memory_service=_BrokenSvc())
    result = await agent.run_turn(session_id="s1", user_message="hi")
    assert result.ok is True


@pytest.mark.asyncio
async def test_no_service_no_block_no_event() -> None:
    """memory_service=None → silent no-op (no block, no event)."""
    llm = _ScriptedLLM(script=[LLMResponse(content="OK")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=llm, bus=bus, memory_service=None)
    await agent.run_turn(session_id="s1", user_message="hi")
    recall_events = [
        e for e in bus.captured
        if getattr(e, "type", None) == EventType.MEMORY_RECALL
    ]
    assert recall_events == []


# ── Write path: auto-extract + remember ──────────────────────────


@pytest.mark.asyncio
async def test_auto_extract_calls_remember_per_candidate() -> None:
    """When LLMFactExtractor returns candidates, hop_loop must
    remember each one and emit one MEMORY_PUT_AUTO per candidate."""
    svc = _make_svc()

    class _StubLLM:
        """Async LLM stub for the extractor — returns a fixed JSON
        array of two candidate facts."""

        async def complete(self, messages, tools=None):  # noqa: ARG002
            class _R:
                content = (
                    '[{"text": "用户决定用 LanceDB", '
                    '"kind": "decision", "scope": "project", '
                    '"confidence": 0.85}, '
                    '{"text": "用户希望简短回答", '
                    '"kind": "preference", "scope": "user", '
                    '"confidence": 0.9}]'
                )
            return _R()

    extractor = LLMFactExtractor(llm=_StubLLM())

    main_llm = _ScriptedLLM(script=[LLMResponse(content="OK 已记住")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=main_llm, bus=bus, memory_service=svc)
    # Mimic the post-construction hot-wire app_lifespan does.
    agent._memory_v2_llm_extractor = extractor

    result = await agent.run_turn(
        session_id="s1",
        user_message="我决定用 LanceDB，请简短一些",
    )
    assert result.ok is True

    # Wait for any background extract tasks to finish.
    import asyncio as _a
    bg = getattr(agent, "_post_sampling_bg", None) or set()
    if bg:
        await _a.wait(bg, timeout=5.0)

    put_events = [
        e for e in bus.captured
        if getattr(e, "type", None) == EventType.MEMORY_PUT_AUTO
    ]
    # 2026-06-09 P3: preference now handled by ExtractLessonsHook,
    # so Layer 2 LLMFactExtractor only emits decision.
    assert len(put_events) == 1
    kinds = {e.payload["kind"] for e in put_events}
    assert kinds == {"decision"}
    # Facts actually landed in the store.
    n = await svc.count()
    assert n >= 1


@pytest.mark.asyncio
async def test_auto_extract_no_op_without_extractor() -> None:
    """No extractor wired → no auto-put events."""
    svc = _make_svc()
    main_llm = _ScriptedLLM(script=[LLMResponse(content="OK")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=main_llm, bus=bus, memory_service=svc)
    # Deliberately do NOT set agent._memory_v2_llm_extractor.
    await agent.run_turn(session_id="s1", user_message="some message")
    put_events = [
        e for e in bus.captured
        if getattr(e, "type", None) == EventType.MEMORY_PUT_AUTO
    ]
    assert put_events == []


@pytest.mark.asyncio
async def test_extractor_failure_does_not_break_turn() -> None:
    """A broken extractor must not crash run_turn — auto-put is a
    background best-effort path."""
    svc = _make_svc()

    class _BoomLLM:
        async def complete(self, messages, tools=None):  # noqa: ARG002
            raise RuntimeError("extractor LLM exploded")

    extractor = LLMFactExtractor(llm=_BoomLLM())
    main_llm = _ScriptedLLM(script=[LLMResponse(content="OK")])
    bus = _CapturingBus()
    agent = AgentLoop(llm=main_llm, bus=bus, memory_service=svc)
    agent._memory_v2_llm_extractor = extractor

    result = await agent.run_turn(session_id="s1", user_message="hi there")
    assert result.ok is True
