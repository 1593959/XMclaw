"""Unit tests for the per-session skill-prefilter LRU cache in AgentLoop.

Covers:
  1. Same message → cache hit on second turn
  2. Different messages → both coexist in cache
  3. Cache full → oldest entry evicted (LRU)
  4. Skill list changes → entire cache invalidated
  5. Accuracy → cached result identical to normal computation
  6. Performance → cache hit is 5×+ faster than cold path
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir.toolcall import ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Message, Pricing
from xmclaw.providers.llm.base import ToolCallShape


# ── minimal mock LLM ───────────────────────────────────────────────────────


@dataclass
class _DummyLLM(LLMProvider):
    model: str = "dummy"

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=())

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        if False:
            yield  # type: ignore[unreachable]

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── helpers ───────────────────────────────────────────────────────────────


def _make_skill_specs(n: int) -> list[ToolSpec]:
    """Build ``n`` skill specs with varying descriptions."""
    return [
        ToolSpec(
            name=f"skill_test_{i}",
            description=f"Test skill number {i} that handles {i} things",
            parameters_schema={"type": "object"},
        )
        for i in range(n)
    ]


def _make_non_skill_specs(n: int) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=f"bash_{i}",
            description=f"bash tool {i}",
            parameters_schema={"type": "object"},
        )
        for i in range(n)
    ]


@pytest.fixture
def agent() -> AgentLoop:
    bus = InProcessEventBus()
    llm = _DummyLLM()
    return AgentLoop(llm=llm, bus=bus)


# ── 1. same message → cache hit ────────────────────────────────────────────


def test_same_message_cache_hit(agent: AgentLoop) -> None:
    specs = _make_skill_specs(400) + _make_non_skill_specs(10)
    msg = "how do I run the test suite"

    # Cold path: cache empty / signature unset → miss
    assert agent._try_skill_prefilter_cache(msg, specs) is None
    # Signature is now set; store a synthetic result
    agent._store_skill_prefilter_cache(msg, specs[:50])

    # Warm path: hit
    cached = agent._try_skill_prefilter_cache(msg, specs)
    assert cached is not None
    assert cached == specs[:50]
    # The hit should have moved the entry to the end of the OrderedDict
    assert list(agent._skill_prefilter_cache.keys())[-1] == agent._get_skill_prefilter_key(msg)


# ── 2. different messages coexist ─────────────────────────────────────────


def test_different_messages_coexist(agent: AgentLoop) -> None:
    specs = _make_skill_specs(400)
    msg1 = "how do I run tests"
    msg2 = "help me write a function"

    # Prime signature
    assert agent._try_skill_prefilter_cache(msg1, specs) is None
    agent._store_skill_prefilter_cache(msg1, specs[:10])
    agent._store_skill_prefilter_cache(msg2, specs[:20])

    assert agent._try_skill_prefilter_cache(msg1, specs) == specs[:10]
    assert agent._try_skill_prefilter_cache(msg2, specs) == specs[:20]


# ── 3. LRU eviction when full ─────────────────────────────────────────────


def test_lru_eviction_when_full(agent: AgentLoop) -> None:
    specs = _make_skill_specs(400)
    maxsize = agent._skill_prefilter_cache_maxsize

    # Prime signature
    assert agent._try_skill_prefilter_cache("msg 0", specs) is None

    # Fill cache to capacity
    for i in range(maxsize):
        agent._store_skill_prefilter_cache(f"msg {i}", [specs[i]])
    assert len(agent._skill_prefilter_cache) == maxsize

    # Access the first entry so it becomes most-recently-used
    agent._try_skill_prefilter_cache("msg 0", specs)

    # Add one more entry → triggers eviction of the oldest (msg 1)
    agent._store_skill_prefilter_cache(f"msg {maxsize}", [specs[maxsize]])

    assert len(agent._skill_prefilter_cache) == maxsize
    assert agent._get_skill_prefilter_key("msg 1") not in agent._skill_prefilter_cache
    assert agent._get_skill_prefilter_key("msg 0") in agent._skill_prefilter_cache
    assert agent._get_skill_prefilter_key(f"msg {maxsize}") in agent._skill_prefilter_cache


# ── 4. skill list change → cache invalidated ────────────────────────────


def test_cache_invalidation_on_skill_change(agent: AgentLoop) -> None:
    specs_400 = _make_skill_specs(400)
    specs_401 = _make_skill_specs(401)
    msg = "run tests"

    # Prime and store with 400 skills
    assert agent._try_skill_prefilter_cache(msg, specs_400) is None
    agent._store_skill_prefilter_cache(msg, specs_400[:10])
    sig_before = agent._skill_prefilter_cache_sig
    assert sig_before != ""

    # Switch to 401 skills → signature mismatch → cache wiped
    cached = agent._try_skill_prefilter_cache(msg, specs_401)
    assert cached is None
    assert len(agent._skill_prefilter_cache) == 0
    sig_after = agent._skill_prefilter_cache_sig
    assert sig_after != sig_before


# ── 5. accuracy: cached result matches normal computation ─────────────────


def test_cached_result_matches_normal_computation(agent: AgentLoop) -> None:
    from xmclaw.skills.prefilter import select_relevant_skills

    specs = _make_skill_specs(400) + _make_non_skill_specs(10)
    msg = "run test suite"

    # Normal cold-path computation
    expected = select_relevant_skills(
        msg,
        specs,
        top_k=12,
        cognitive_state=agent._cognitive_state,
    )

    # Store and retrieve via cache
    agent._skill_prefilter_cache_sig = agent._get_skill_prefilter_signature(specs)
    agent._store_skill_prefilter_cache(msg, expected)
    cached = agent._try_skill_prefilter_cache(msg, specs)

    assert cached is not None
    assert [getattr(s, "name", "") for s in cached] == [
        getattr(s, "name", "") for s in expected
    ]


# ── 6. performance: 5×+ speedup on cache hit ──────────────────────────────


def test_cache_hit_is_faster_than_cold_path(agent: AgentLoop) -> None:
    from xmclaw.skills.prefilter import select_relevant_skills

    specs = _make_skill_specs(400) + _make_non_skill_specs(10)
    msg = "run test suite"
    n = 100

    # Cold path: run select_relevant_skills repeatedly
    t0 = time.perf_counter()
    for _ in range(n):
        select_relevant_skills(
            msg,
            specs,
            top_k=12,
            cognitive_state=agent._cognitive_state,
        )
    cold_time = time.perf_counter() - t0

    # Warm path: cached lookup (pre-compute signature so the loop mirrors
    # the real run_turn path where sig is computed once outside the method).
    agent._skill_prefilter_cache_sig = agent._get_skill_prefilter_signature(specs)
    agent._store_skill_prefilter_cache(msg, specs[:50])
    t0 = time.perf_counter()
    for _ in range(n):
        agent._try_skill_prefilter_cache(msg, specs, sig=agent._skill_prefilter_cache_sig)
    warm_time = time.perf_counter() - t0

    cold_per = cold_time / n
    warm_per = warm_time / n
    ratio = cold_per / warm_per
    assert ratio > 5.0, (
        f"Cache hit was only {ratio:.1f}× faster per iteration "
        f"(cold={cold_per:.6f}s, warm={warm_per:.6f}s)"
    )
