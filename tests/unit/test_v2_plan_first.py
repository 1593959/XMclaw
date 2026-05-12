"""Unit tests for PlanFirstGate (Batch B.1)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from xmclaw.cognition.plan_first import PlanFirstGate, _parse_plan_steps


class _StubLLM:
    """LLM stub that returns whatever ``response`` you set."""

    def __init__(self, response: str = "", raises: Exception | None = None,
                 latency_s: float = 0.0):
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

        class _Resp:
            content = self.response  # type: ignore[attr-defined]
        return _Resp()


# ── Heuristic complexity classifier ──────────────────────────────


@pytest.mark.parametrize("msg", [
    "what time is it",
    "hi",
    "thanks",
    "可以再来一次吗",
    "ok",
])
def test_is_complex_rejects_trivial(msg):
    gate = PlanFirstGate(llm=_StubLLM())
    assert gate.is_complex(msg) is False


@pytest.mark.parametrize("msg", [
    "First find all TODOs in the codebase, then read each file and "
    "summarize the most urgent ones for me.",
    "1. Read the README. 2. Find the entry point. 3. Run the tests.",
    "首先帮我搜索一下相关文件，然后读取每个文件的内容，最后生成一个报告。",
    "Please analyze the architecture, compare it with the documentation, "
    "verify that all modules are tested, and generate a summary report.",
])
def test_is_complex_accepts_multistep(msg):
    gate = PlanFirstGate(llm=_StubLLM())
    assert gate.is_complex(msg) is True


def test_is_complex_uses_length_as_signal():
    """Very long messages auto-qualify even without explicit markers."""
    gate = PlanFirstGate(llm=_StubLLM(), threshold=2)
    long_msg = "Please help me with " + "details " * 70  # ~600 chars
    assert gate.is_complex(long_msg) is True


def test_is_complex_handles_non_string():
    gate = PlanFirstGate(llm=_StubLLM())
    assert gate.is_complex(None) is False  # type: ignore[arg-type]
    assert gate.is_complex(123) is False  # type: ignore[arg-type]


# ── Plan generation ──────────────────────────────────────────────


async def test_plan_returns_list_from_json():
    llm = _StubLLM(response='["Step 1", "Step 2", "Step 3"]')
    gate = PlanFirstGate(llm=llm)
    steps = await gate.plan("test request")
    assert steps == ["Step 1", "Step 2", "Step 3"]
    assert llm.calls == 1


async def test_plan_strips_json_fence():
    llm = _StubLLM(response='```json\n["a", "b"]\n```')
    gate = PlanFirstGate(llm=llm)
    assert await gate.plan("x") == ["a", "b"]


async def test_plan_caps_steps():
    llm = _StubLLM(
        response='[' + ', '.join(f'"s{i}"' for i in range(15)) + ']',
    )
    gate = PlanFirstGate(llm=llm, max_steps=5)
    steps = await gate.plan("x")
    assert len(steps) == 5


async def test_plan_truncates_long_step():
    long_step = "X" * 300
    llm = _StubLLM(response=f'["short", "{long_step}"]')
    gate = PlanFirstGate(llm=llm)
    steps = await gate.plan("x")
    assert len(steps[1]) <= 240
    assert "…" in steps[1]


async def test_plan_empty_on_timeout():
    llm = _StubLLM(response='["s"]', latency_s=2.0)
    gate = PlanFirstGate(llm=llm, timeout_s=0.3)
    steps = await gate.plan("x")
    assert steps == []


async def test_plan_empty_on_llm_failure():
    llm = _StubLLM(raises=RuntimeError("boom"))
    gate = PlanFirstGate(llm=llm)
    steps = await gate.plan("x")
    assert steps == []


async def test_plan_empty_on_no_llm():
    gate = PlanFirstGate(llm=None)
    steps = await gate.plan("x")
    assert steps == []


async def test_plan_falls_back_to_markdown_list():
    """When LLM returns prose with bullet list (instead of JSON), still
    parse out the steps."""
    llm = _StubLLM(response=(
        "Sure, here are the steps:\n\n"
        "1. First do A\n"
        "2. Then do B\n"
        "3. Finally do C\n"
    ))
    gate = PlanFirstGate(llm=llm)
    steps = await gate.plan("x")
    assert len(steps) == 3
    assert "First do A" in steps[0]


async def test_plan_handles_object_with_steps_key():
    """LLM sometimes wraps the array in {\"steps\": [...]}."""
    llm = _StubLLM(response='{"steps": ["a", "b", "c"]}')
    gate = PlanFirstGate(llm=llm)
    assert await gate.plan("x") == ["a", "b", "c"]


# ── Parser unit tests ────────────────────────────────────────────


def test_parse_strict_json():
    assert _parse_plan_steps('["a","b"]', max_steps=5) == ["a", "b"]


def test_parse_fenced_json():
    assert _parse_plan_steps('```json\n["x"]\n```', max_steps=5) == ["x"]


def test_parse_bullets():
    raw = "1. step one\n2. step two\n- step three"
    out = _parse_plan_steps(raw, max_steps=5)
    assert out == ["step one", "step two", "step three"]


def test_parse_empty():
    assert _parse_plan_steps("", max_steps=5) == []
    assert _parse_plan_steps("just prose, nothing parseable", max_steps=5) == []


def test_parse_filters_empty_strings():
    assert _parse_plan_steps('["valid", "", "  ", "also valid"]', max_steps=5) == [
        "valid", "also valid",
    ]
