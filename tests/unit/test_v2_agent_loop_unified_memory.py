"""AgentLoop ↔ UnifiedMemorySystem integration ("agent 自己用记忆").

Pins the wiring shipped 2026-05-10 in response to user feedback:
"我的目的是给他自己用，不是光给我用." Pre-this-commit the unified
memory system existed (router + UI tab) but ``AgentLoop`` never
called it. Now:

  Phase A (read):  ``run_turn`` calls ``unified_memory.query`` at
                   the start, injects hits into the user message,
                   emits MEMORY_RECALL.
  Phase B (write): post-turn, ``MemoryExtractor`` heuristic-gates +
                   LLM-extracts a durable fact, calls
                   ``unified_memory.put``, emits MEMORY_PUT_AUTO.

These tests pin both directions end-to-end against fakes so a future
refactor that breaks either path fails immediately. They DO NOT test
the LLM extract details — that's covered in test_v2_memory_extractor.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.memory import ExtractedFact, MemoryEntry
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
    """Returns canned responses + records what messages it saw —
    so tests can inspect whether the user content carried the
    ``<unified-recall>`` block."""

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


@dataclass
class _FakeUnifiedMemory:
    """Stand-in for UnifiedMemorySystem with controllable hit list +
    recorded put calls. Mirrors the real system's async interface."""

    canned_hits: list[MemoryEntry] = field(default_factory=list)
    put_calls: list[dict[str, Any]] = field(default_factory=list)
    next_put_id: str = "fake-id-1"

    async def query(
        self, *,
        semantic: str | None = None,
        relation: str | None = None,
        temporal: Any = None,
        layer: Any = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        # Echo hits regardless of axes — tests script the canned list.
        return list(self.canned_hits[:limit])

    async def put(
        self, *,
        text: str,
        layer: str = "long_term",
        node_type: str = "event",
        relations: Any = None,
        metadata: Any = None,
        embedding: Any = None,
    ) -> str:
        self.put_calls.append({
            "text": text, "layer": layer, "node_type": node_type,
            "metadata": metadata,
        })
        return self.next_put_id


@dataclass
class _ScriptedExtractor:
    """Stub MemoryExtractor — yields whatever extracted_fact the test
    sets, with a call counter for assertions."""

    extracted_fact: ExtractedFact | None = None
    calls: int = 0
    last_user: str = ""
    last_assistant: str = ""

    async def extract(
        self, *,
        user_message: str,
        assistant_response: str,
    ) -> ExtractedFact | None:
        self.calls += 1
        self.last_user = user_message
        self.last_assistant = assistant_response
        return self.extracted_fact


# ── helpers ──────────────────────────────────────────────────────


class _CapturingBus(InProcessEventBus):
    """Records every published event for post-turn assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.captured: list[Any] = []

    async def publish(self, event):  # noqa: ANN001
        self.captured.append(event)
        return await super().publish(event)


def _make_entry(eid: str, text: str, score: float, axes: tuple) -> MemoryEntry:
    return MemoryEntry(
        id=eid, layer="long_term", text=text, score=score,
        created_at=1000.0, metadata={}, matched_axes=axes,
    )


# ── Phase A: recall path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_a_recall_injects_unified_block_into_user_message() -> None:
    """When unified_memory returns hits, the LLM's user content MUST
    carry a ``<unified-recall>`` block. The matched_axes string must
    appear so the LLM sees WHICH axis matched."""
    mem = _FakeUnifiedMemory(canned_hits=[
        _make_entry("e1", "user prefers vim", 0.85, ("semantic",)),
        _make_entry("e2", "project X uses python", 0.6, ("semantic", "relation")),
    ])
    llm = _ScriptedLLM(script=[LLMResponse(content="ok")])
    agent = AgentLoop(
        llm=llm, bus=_CapturingBus(),
        unified_memory=mem,
    )
    await agent.run_turn("sess-A", "tell me about X")
    # The last LLM call's user message should carry the block.
    assert llm.seen_messages, "agent never called the LLM"
    user_msg = next(
        (m for m in reversed(llm.seen_messages[-1]) if m.role == "user"),
        None,
    )
    assert user_msg is not None
    content = user_msg.content
    assert "<unified-recall>" in content
    assert "user prefers vim" in content
    assert "project X uses python" in content
    # Axes must surface — semantic/relation badge is the value-add
    # over the legacy memory_ctx_block.
    assert "semantic" in content
    assert "relation" in content


@pytest.mark.asyncio
async def test_phase_a_emits_memory_recall_event() -> None:
    """MEMORY_RECALL event MUST fire — the UI's 记忆活动 timeline
    listens for it. Even when hits=[] it must fire (so the timeline
    can show "agent queried but found nothing")."""
    mem = _FakeUnifiedMemory(canned_hits=[
        _make_entry("e1", "user prefers vim", 0.85, ("semantic",)),
    ])
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="ok")])
    agent = AgentLoop(llm=llm, bus=bus, unified_memory=mem)
    await agent.run_turn("sess-A2", "what do I use?")

    recalls = [
        e for e in bus.captured
        if e.type == EventType.MEMORY_RECALL
    ]
    assert len(recalls) == 1
    payload = recalls[0].payload
    assert payload["session_id"] == "sess-A2"
    assert payload["query"] == "what do I use?"
    assert payload["limit"] == 5  # default unified_recall_top_k
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["id"] == "e1"
    assert payload["hits"][0]["matched_axes"] == ["semantic"]


@pytest.mark.asyncio
async def test_phase_a_emits_recall_event_even_when_no_hits() -> None:
    """Empty-hit recall still emits the event so the UI shows the
    query DID run — important for "agent doesn't appear to use memory"
    debugging."""
    mem = _FakeUnifiedMemory(canned_hits=[])
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="ok")])
    agent = AgentLoop(llm=llm, bus=bus, unified_memory=mem)
    await agent.run_turn("sess-A3", "anything?")
    recalls = [e for e in bus.captured if e.type == EventType.MEMORY_RECALL]
    assert len(recalls) == 1
    assert recalls[0].payload["hits"] == []


@pytest.mark.asyncio
async def test_phase_a_no_unified_memory_no_block_no_event() -> None:
    """Backward compat: when ``unified_memory`` is None (default for
    pre-2026-05-10 callers), no block is added + no event fires.
    Zero behaviour change for legacy deployments."""
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="ok")])
    agent = AgentLoop(llm=llm, bus=bus)  # unified_memory not passed
    await agent.run_turn("sess-A4", "hi")
    user_msg = next(
        (m for m in reversed(llm.seen_messages[-1]) if m.role == "user"),
        None,
    )
    assert user_msg is not None
    assert "<unified-recall>" not in user_msg.content
    recalls = [e for e in bus.captured if e.type == EventType.MEMORY_RECALL]
    assert recalls == []


@pytest.mark.asyncio
async def test_phase_a_recall_failure_does_not_break_turn() -> None:
    """If the unified store raises, the turn MUST still complete —
    recall is best-effort. Pin this so a faulty memory backend
    doesn't take down user-visible turns."""

    class _FlakyMem:
        async def query(self, **_: Any) -> list:
            raise RuntimeError("vec store offline")

        async def put(self, **_: Any) -> str:
            return "x"

    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="actual answer")])
    agent = AgentLoop(llm=llm, bus=bus, unified_memory=_FlakyMem())
    result = await agent.run_turn("sess-A5", "hello")
    assert result.ok
    assert result.text == "actual answer"


# ── Phase B: auto-put path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_b_auto_put_when_extractor_yields_fact() -> None:
    """Extractor returns a fact → ``unified_memory.put`` is called +
    MEMORY_PUT_AUTO event fires."""
    fact = ExtractedFact(
        text="User prefers vim",
        node_type="entity",
        layer="long_term",
        reason="explicit preference",
    )
    extractor = _ScriptedExtractor(extracted_fact=fact)
    mem = _FakeUnifiedMemory(next_put_id="put-id-1")
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="okay, vim it is")])
    agent = AgentLoop(
        llm=llm, bus=bus,
        unified_memory=mem,
        memory_extractor=extractor,
    )
    await agent.run_turn("sess-B1", "我习惯用 vim")
    # Extractor + put run in a background task — let it land.
    # Tasks are tracked in self._post_sampling_bg.
    while agent._post_sampling_bg:
        await asyncio.sleep(0.01)

    assert extractor.calls == 1
    assert extractor.last_user == "我习惯用 vim"
    assert extractor.last_assistant == "okay, vim it is"
    assert len(mem.put_calls) == 1
    pc = mem.put_calls[0]
    assert pc["text"] == "User prefers vim"
    assert pc["layer"] == "long_term"
    assert pc["node_type"] == "entity"
    # Metadata MUST carry the source marker so the UI can distinguish
    # auto-extracted facts from operator-typed ones in /ui/memory.
    assert pc["metadata"]["source"] == "auto_extract"
    assert pc["metadata"]["session_id"] == "sess-B1"

    puts = [e for e in bus.captured if e.type == EventType.MEMORY_PUT_AUTO]
    assert len(puts) == 1
    assert puts[0].payload["id"] == "put-id-1"
    assert puts[0].payload["text"] == "User prefers vim"


@pytest.mark.asyncio
async def test_phase_b_no_put_when_extractor_yields_none() -> None:
    """Extractor returned None (no fact) → no put, no event. Pin
    this so a future refactor that "always puts something" doesn't
    silently flood the store."""
    extractor = _ScriptedExtractor(extracted_fact=None)
    mem = _FakeUnifiedMemory()
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="response")])
    agent = AgentLoop(
        llm=llm, bus=bus,
        unified_memory=mem, memory_extractor=extractor,
    )
    await agent.run_turn("sess-B2", "天气怎么样")
    while agent._post_sampling_bg:
        await asyncio.sleep(0.01)
    assert extractor.calls == 1
    assert mem.put_calls == [], (
        "extractor returned None but put fired anyway — store "
        "would fill with noise"
    )
    puts = [e for e in bus.captured if e.type == EventType.MEMORY_PUT_AUTO]
    assert puts == []


@pytest.mark.asyncio
async def test_phase_b_no_extractor_no_call() -> None:
    """When ``memory_extractor`` is None, the auto-put path is a
    silent no-op. Backward compat: pre-2026-05-10 callers see zero
    change."""
    mem = _FakeUnifiedMemory()
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="response")])
    agent = AgentLoop(
        llm=llm, bus=bus,
        unified_memory=mem,
        # memory_extractor not passed
    )
    await agent.run_turn("sess-B3", "记住 X")
    while agent._post_sampling_bg:
        await asyncio.sleep(0.01)
    assert mem.put_calls == []


@pytest.mark.asyncio
async def test_phase_b_extractor_failure_does_not_break_turn() -> None:
    """A blowing-up extractor MUST NOT propagate up. Same posture as
    Phase A: best-effort, errors logged + swallowed."""

    class _BoomExtractor:
        async def extract(self, **_: Any) -> Any:
            raise RuntimeError("extractor exploded")

    mem = _FakeUnifiedMemory()
    bus = _CapturingBus()
    llm = _ScriptedLLM(script=[LLMResponse(content="response")])
    agent = AgentLoop(
        llm=llm, bus=bus,
        unified_memory=mem, memory_extractor=_BoomExtractor(),
    )
    result = await agent.run_turn("sess-B4", "hi")
    while agent._post_sampling_bg:
        await asyncio.sleep(0.01)
    assert result.ok
    # No put happened (extractor failed before producing a fact).
    assert mem.put_calls == []
    # No MEMORY_PUT_AUTO event either.
    puts = [e for e in bus.captured if e.type == EventType.MEMORY_PUT_AUTO]
    assert puts == []
