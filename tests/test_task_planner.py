"""Regression tests for TaskPlanner.

The primary bug this file pins down:

  Running any medium/high-complexity task crashed the agent with
  ``'dict' object has no attribute 'complexity'``. Root cause: the
  planner did ``profile.complexity`` (attribute access), but
  ``TaskProfile`` is a ``TypedDict`` — a plain dict at runtime. The
  classifier returns values as ``str, Enum`` members on the fresh path,
  but any JSON round-trip (resume cache, persistence) collapses them to
  plain strings. So the access form AND the value shape both matter.

  Fix: read fields via the helper that accepts both, and compare against
  ``Complexity.LOW`` / ``Complexity.MEDIUM`` — these inherit ``str`` so
  equality is string-based regardless of the value's original shape.

The tests cover both value shapes to guard against regressions that fix
only one path.
"""
from __future__ import annotations

import pytest

from xmclaw.core.task_classifier import (
    ClassifierSource,
    Complexity,
    TaskProfile,
    TaskType,
)
from xmclaw.core.task_planner import TaskPlanner


class _NoopLLM:
    """Minimal LLMRouter stub — LOW-complexity tests never call ``.stream``.

    Any test that exercises the medium/high path must patch this with a
    real async generator; we fail loudly if a test unexpectedly reaches it.
    """
    async def stream(self, messages):  # pragma: no cover - guard
        raise AssertionError("planner should not call LLM for LOW complexity")


def _enum_profile() -> TaskProfile:
    """TaskProfile with Enum values — the shape classifier emits directly."""
    return TaskProfile(
        type=TaskType.PLAN,
        complexity=Complexity.LOW,
        capabilities_needed=["research"],
        recommended_actions=["plan_steps"],
        reasoning="analyze-structure",
        subtasks=[],
        source=ClassifierSource.LLM,
    )


def _string_profile() -> TaskProfile:
    """TaskProfile with plain string values — shape after JSON round-trip."""
    return {  # type: ignore[return-value]
        "type": "plan",
        "complexity": "low",
        "capabilities_needed": ["research"],
        "recommended_actions": ["plan_steps"],
        "reasoning": "analyze-structure",
        "subtasks": [],
        "source": "llm",
    }


@pytest.mark.asyncio
async def test_plan_low_complexity_enum_profile():
    """Fresh classifier output (Enum values) must not crash the planner."""
    planner = TaskPlanner(_NoopLLM())  # type: ignore[arg-type]
    plan = await planner.plan("what is 2+2", _enum_profile())
    assert plan["steps"][0]["step"] == 1
    assert plan["estimated_steps"] == 1


@pytest.mark.asyncio
async def test_plan_low_complexity_string_profile():
    """Resumed/persisted profile (plain strings) must not crash the planner.

    This is the exact shape that triggered ``'dict' object has no
    attribute 'complexity'`` in production.
    """
    planner = TaskPlanner(_NoopLLM())  # type: ignore[arg-type]
    plan = await planner.plan("what is 2+2", _string_profile())
    assert plan["steps"][0]["step"] == 1


@pytest.mark.asyncio
async def test_plan_medium_complexity_string_profile_reaches_llm():
    """Medium complexity on a string-valued profile must dispatch to the LLM
    path with the string values formatted into the prompt — not crash on
    ``.type.value`` / ``.complexity.value`` attribute access.
    """
    captured_prompts: list[str] = []

    class _CaptureLLM:
        async def stream(self, messages):
            captured_prompts.append(messages[-1]["content"])
            yield (
                '{"steps": [{"step": 1, "action": "a", "tool": "", '
                '"reasoning": "r", "depends_on": []}], '
                '"estimated_steps": 1, "needs_confirmation": false, '
                '"reasoning": "test"}'
            )

    profile: TaskProfile = {  # type: ignore[assignment]
        "type": "plan",
        "complexity": "medium",
        "capabilities_needed": [],
        "recommended_actions": [],
        "reasoning": "r",
        "subtasks": [],
        "source": "llm",
    }
    planner = TaskPlanner(_CaptureLLM())  # type: ignore[arg-type]
    plan = await planner.plan("分析项目", profile)
    assert plan["estimated_steps"] == 1
    assert captured_prompts, "LLM path must run for medium complexity"
    assert "plan" in captured_prompts[0]
    assert "medium" in captured_prompts[0]
