"""Phase 3.2 — LLMFactExtractor unit tests.

Uses a stub LLM that returns canned JSON so the test suite runs
fast + deterministic (no real network).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
)
from xmclaw.memory.v2.llm_extractor import (
    LLMFactExtractor,
    llm_extract_and_remember,
)


# ── Stub LLM ─────────────────────────────────────────────────────


@dataclass
class _StubResp:
    content: str


class _StubLLM:
    """Returns whatever JSON the test passes in. ``raises=True`` to
    simulate provider failure."""

    def __init__(
        self,
        payload: list[dict[str, Any]] | str | None = None,
        *,
        raises: bool = False,
        delay: float = 0.0,
    ) -> None:
        self._payload = payload
        self._raises = raises
        self._delay = delay
        self.calls = 0

    async def complete(self, messages, tools=None):  # noqa: D401
        import asyncio
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("simulated provider failure")
        if isinstance(self._payload, str):
            content = self._payload
        elif self._payload is None:
            content = "[]"
        else:
            content = json.dumps(self._payload, ensure_ascii=False)
        return _StubResp(content=content)


# ── Happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_returns_facts_from_stub() -> None:
    llm = _StubLLM([
        {
            "text": "用户做陪玩店业务",
            "kind": "identity",
            "scope": "user",
            "confidence": 0.9,
        },
        {
            "text": "目标月流水破 10 万",
            "kind": "project",
            "scope": "project",
            "confidence": 0.85,
        },
    ])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("我是干陪玩店的，目标月流水 10 万")
    assert len(facts) == 2
    assert facts[0]["text"] == "用户做陪玩店业务"
    assert facts[0]["kind"] == "identity"


@pytest.mark.asyncio
async def test_extract_empty_array_returns_empty() -> None:
    llm = _StubLLM([])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("你好")
    # Short messages skip the LLM call entirely
    assert facts == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_extract_short_message_skipped() -> None:
    """< 8 chars → no LLM call (avoid noise + cost on 'ok' / 'yes')."""
    llm = _StubLLM([])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("好的")
    assert facts == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_extract_empty_input() -> None:
    llm = _StubLLM([])
    ex = LLMFactExtractor(llm)
    assert await ex.extract("") == []
    assert await ex.extract("   ") == []


# ── Validation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_rejects_invalid_kind() -> None:
    llm = _StubLLM([
        {"text": "X", "kind": "garbage", "scope": "user", "confidence": 0.8},
        {"text": "Y", "kind": "decision", "scope": "user", "confidence": 0.8},
    ])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    assert len(facts) == 1
    assert facts[0]["text"] == "Y"


@pytest.mark.asyncio
async def test_extract_coerces_invalid_scope() -> None:
    llm = _StubLLM([
        {"text": "X", "kind": "decision", "scope": "BOGUS", "confidence": 0.8},
    ])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    # Invalid scope coerced to 'project' default.
    assert facts[0]["scope"] == "project"


@pytest.mark.asyncio
async def test_extract_clamps_confidence() -> None:
    llm = _StubLLM([
        {"text": "low", "kind": "decision", "scope": "user", "confidence": -0.5},
        {"text": "high", "kind": "decision", "scope": "user", "confidence": 1.7},
        {"text": "bad", "kind": "decision", "scope": "user", "confidence": "abc"},
    ])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    confs = [f["confidence"] for f in facts]
    assert all(0.5 <= c <= 0.95 for c in confs)


@pytest.mark.asyncio
async def test_extract_empty_text_rejected() -> None:
    llm = _StubLLM([
        {"text": "", "kind": "decision", "scope": "user", "confidence": 0.8},
        {"text": "   ", "kind": "decision", "scope": "user", "confidence": 0.8},
        {"text": "valid", "kind": "decision", "scope": "user", "confidence": 0.8},
    ])
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    assert len(facts) == 1
    assert facts[0]["text"] == "valid"


# ── Failure modes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_llm_failure_returns_empty() -> None:
    llm = _StubLLM(raises=True)
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    assert facts == []


@pytest.mark.asyncio
async def test_extract_bad_json_returns_empty() -> None:
    llm = _StubLLM("not json at all { malformed")
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    assert facts == []


@pytest.mark.asyncio
async def test_extract_strips_markdown_fence() -> None:
    """LLM sometimes wraps in ```json ``` despite instruction."""
    llm = _StubLLM(
        '```json\n[{"text": "X", "kind": "decision", "scope": "user", "confidence": 0.8}]\n```'
    )
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_extract_non_array_returns_empty() -> None:
    """LLM might return a single object instead of array."""
    llm = _StubLLM('{"text": "X", "kind": "decision", "scope": "user", "confidence": 0.8}')
    ex = LLMFactExtractor(llm)
    facts = await ex.extract("test message with content here")
    # We require array — reject single object.
    assert facts == []


@pytest.mark.asyncio
async def test_extract_timeout_returns_empty() -> None:
    """LLM stalls past timeout_s → empty list, no exception."""
    llm = _StubLLM([{"text": "X", "kind": "decision", "scope": "user", "confidence": 0.8}], delay=2.0)
    ex = LLMFactExtractor(llm, timeout_s=0.1)
    facts = await ex.extract("test message with content here")
    assert facts == []


# ── End-to-end: llm_extract_and_remember ─────────────────────────


@pytest.mark.asyncio
async def test_extract_and_remember_writes_to_service() -> None:
    llm = _StubLLM([
        {"text": "用户做陪玩店", "kind": "identity", "scope": "user", "confidence": 0.9},
        {"text": "月流水目标 10 万", "kind": "project", "scope": "project", "confidence": 0.85},
    ])
    ex = LLMFactExtractor(llm)
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    written = await llm_extract_and_remember(
        "我是干陪玩店的，目标月流水 10 万",
        svc, ex, source_event_id="ev-001",
    )
    assert len(written) == 2
    assert await svc.count() == 2
    # CAUSED_BY edge wired
    for f in written:
        nbrs = await svc.neighbors(f.id, relation_types=["CAUSED_BY"])
        assert any(t == "event:ev-001" for _, t in nbrs)


@pytest.mark.asyncio
async def test_complementary_with_regex_extractor() -> None:
    """The big-picture test: regex captures URL/account, LLM captures
    the implicit 'user does X business' fact that regex can't see."""
    from xmclaw.memory.v2 import extract_and_remember

    msg = (
        "我是干陪玩店的，网站 https://pw310.wxselling.com，"
        "我们的客单价大约 50 元。"
    )
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )

    # Layer 1: regex catches URL.
    regex_written = await extract_and_remember(msg, svc)
    regex_texts = {f.text for f in regex_written}
    assert any("pw310" in t for t in regex_texts)

    # Layer 2: LLM catches implicit "陪玩店业务" identity that
    # regex's "我是 X" pattern can also catch (but LLM gives richer
    # text + sometimes captures the industry/business model).
    llm = _StubLLM([
        {
            "text": "用户经营陪玩店业务",
            "kind": "identity",
            "scope": "user",
            "confidence": 0.85,
        },
        {
            "text": "客单价约 50 元",
            "kind": "project",
            "scope": "project",
            "confidence": 0.8,
        },
    ])
    ex = LLMFactExtractor(llm)
    llm_written = await llm_extract_and_remember(msg, svc, ex)
    # Both write paths succeed; total fact count is the union (no
    # double-count if id derivations overlap).
    assert len(llm_written) == 2
    assert await svc.count() >= 3  # URL + 陪玩店 + 客单价
