"""Unit tests for StepValidator (Batch C.2)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xmclaw.cognition.step_validator import (
    StepValidator,
    StepVerdict,
    _excerpt,
    _parse_verdict_json,
)


class _LLMResp:
    def __init__(self, content: str = "") -> None:
        self.content = content


class _StubLLM:
    def __init__(
        self,
        response: str = "",
        raises: Exception | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.response = response
        self.raises = raises
        self.latency_s = latency_s
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.raises:
            raise self.raises
        return _LLMResp(content=self.response)


# ── enabled-flag / disabled passthrough ──────────────────────────


async def test_disabled_returns_none():
    sv = StepValidator(llm=_StubLLM(response='{"verdict":"advance"}'),
                       enabled=False)
    v = await sv.validate(
        goal="g", plan_steps=None, tool_name="x", tool_args={},
        tool_result="result",
    )
    assert v is None
    assert sv.enabled is False


async def test_enabled_but_no_llm_returns_none():
    sv = StepValidator(llm=None, enabled=True)
    assert sv.enabled is False
    v = await sv.validate(
        goal="g", plan_steps=None, tool_name="x", tool_args={},
        tool_result="result",
    )
    assert v is None


async def test_late_binding_enabled_after_set_llm():
    sv = StepValidator(llm=None, enabled=False)
    sv.set_llm(_StubLLM(response='{"verdict":"advance","confidence":0.9}'))
    sv.set_enabled(True)
    assert sv.enabled is True
    v = await sv.validate(
        goal="g", plan_steps=None, tool_name="x", tool_args={},
        tool_result="r",
    )
    assert v is not None
    assert v.verdict == "advance"


# ── verdict shapes ───────────────────────────────────────────────


async def test_advance_verdict():
    sv = StepValidator(
        llm=_StubLLM(response=json.dumps({
            "verdict": "advance", "confidence": 0.85,
            "reason": "found target",
        })),
        enabled=True,
    )
    v = await sv.validate(
        goal="find foo", plan_steps=None, tool_name="grep",
        tool_args={"pattern": "foo"}, tool_result="match at line 12",
    )
    assert v.verdict == "advance"
    assert v.confidence == 0.85
    assert "found target" in v.reason


async def test_neutral_verdict():
    sv = StepValidator(
        llm=_StubLLM(response='{"verdict":"neutral","confidence":0.4,"reason":"unrelated"}'),
        enabled=True,
    )
    v = await sv.validate(
        goal="g", plan_steps=["step1"], tool_name="t",
        tool_args={}, tool_result="r",
    )
    assert v.verdict == "neutral"


async def test_regress_verdict():
    sv = StepValidator(
        llm=_StubLLM(response='{"verdict":"regress","confidence":0.7,"reason":"broke build"}'),
        enabled=True,
    )
    v = await sv.validate(
        goal="g", plan_steps=None, tool_name="t",
        tool_args={}, tool_result="error: build failed",
    )
    assert v.verdict == "regress"


async def test_stats_track_each_verdict():
    sv = StepValidator(
        llm=_StubLLM(response='{"verdict":"advance"}'),
        enabled=True,
    )
    for _ in range(3):
        await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert sv.stats()["advance"] == 3
    assert sv.stats()["regress"] == 0


# ── tolerant verdict parsing ────────────────────────────────────


async def test_unknown_verdict_string_returns_none():
    sv = StepValidator(
        llm=_StubLLM(response='{"verdict":"maybe-good","confidence":0.5}'),
        enabled=True,
    )
    v = await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert v is None
    assert sv.stats()["failed"] == 1


async def test_bad_json_returns_none():
    sv = StepValidator(
        llm=_StubLLM(response="not json"),
        enabled=True,
    )
    v = await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert v is None


async def test_confidence_clamped():
    sv = StepValidator(
        llm=_StubLLM(response='{"verdict":"advance","confidence":2.5}'),
        enabled=True,
    )
    v = await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert v.confidence == 1.0

    sv2 = StepValidator(
        llm=_StubLLM(response='{"verdict":"advance","confidence":"not-a-number"}'),
        enabled=True,
    )
    v2 = await sv2.validate(goal="g", plan_steps=None, tool_name="t",
                            tool_args={}, tool_result="r")
    assert v2.confidence == 0.5  # fallback


async def test_fenced_json():
    sv = StepValidator(
        llm=_StubLLM(response='```json\n{"verdict":"advance"}\n```'),
        enabled=True,
    )
    v = await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert v is not None
    assert v.verdict == "advance"


# ── failure modes never raise ───────────────────────────────────


async def test_llm_timeout_returns_none():
    sv = StepValidator(
        llm=_StubLLM(response='{"verdict":"advance"}', latency_s=2.0),
        enabled=True,
        timeout_s=0.3,
    )
    v = await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert v is None
    assert sv.stats()["failed"] == 1


async def test_llm_raises_returns_none():
    sv = StepValidator(
        llm=_StubLLM(raises=RuntimeError("boom")),
        enabled=True,
    )
    v = await sv.validate(goal="g", plan_steps=None, tool_name="t",
                          tool_args={}, tool_result="r")
    assert v is None


# ── helper parsers ──────────────────────────────────────────────


def test_excerpt_short_passes_through():
    assert _excerpt("hi", 100) == "hi"


def test_excerpt_long_keeps_head_and_tail():
    s = "A" * 200 + "Z" * 200
    out = _excerpt(s, 100)
    assert "A" in out
    assert "Z" in out
    assert "[truncated]" in out
    assert len(out) <= 200  # head + tail + marker


def test_excerpt_empty():
    assert _excerpt("", 100) == "(empty result)"


def test_parse_verdict_strict_json():
    assert _parse_verdict_json('{"verdict":"advance"}') == {"verdict": "advance"}


def test_parse_verdict_rejects_no_verdict_field():
    assert _parse_verdict_json('{"foo":"bar"}') is None


def test_parse_verdict_empty():
    assert _parse_verdict_json("") is None
    assert _parse_verdict_json("just prose") is None
