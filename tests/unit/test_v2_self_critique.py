from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from xmclaw.cognition.self_critique import (
    CRITIQUE_DIMENSIONS,
    SelfCritiqueEngine,
    SelfCritiqueMaterializer,
    SelfCritiqueMemoryPolicy,
    SelfCritiquePromptBuilder,
    SelfCritiqueRequest,
    TrajectoryEvent,
    parse_self_critique_json,
)


@dataclass
class _FakeFact:
    id: str


class _FakeMemoryService:
    def __init__(self) -> None:
        self.calls = []

    async def remember(self, text: str, **kwargs):
        self.calls.append((text, kwargs))
        return _FakeFact(id="lesson:project:abc")


def _memory_worthy_critique():
    return parse_self_critique_json(
        json.dumps(
            {
                "trigger": "tool_error",
                "diagnosis": "The agent retried a failed tool call.",
                "dimension_scores": {},
                "lesson": (
                    "When the same tool error repeats, change strategy "
                    "before retrying again."
                ),
                "retry_decision": "change_plan",
                "memory_worthy": True,
                "confidence": 0.85,
            },
        ),
    )


def test_self_critique_prompt_contains_reflexion_dimensions() -> None:
    prompt = SelfCritiquePromptBuilder().build(
        SelfCritiqueRequest(
            trigger="tool_error",
            session_id="s1",
            goal="fix failing tests",
            failure_summary="bash failed with non-zero exit",
            trajectory=(
                TrajectoryEvent(
                    kind="tool_call",
                    tool_name="bash",
                    ok=False,
                    error="exit 1",
                    content="pytest tests/unit/test_x.py",
                ),
            ),
            graph_state={"final": "failed", "errors": [{"message": "exit 1"}]},
        ),
    )

    assert "Reflexion critic" in prompt
    for dimension in CRITIQUE_DIMENSIONS:
        assert dimension in prompt
    assert "tool_error" in prompt
    assert "pytest tests/unit/test_x.py" in prompt
    assert "strict JSON only" in prompt


def test_parse_self_critique_json_normalizes_scores_and_retry_decision() -> None:
    raw = json.dumps(
        {
            "trigger": "max_hops_exit",
            "diagnosis": "Looped without checking progress.",
            "dimension_scores": {
                "plan_quality": 1.2,
                "tool_choice": 0.5,
                "evidence": -1,
            },
            "lesson": "Add an explicit progress check before another hop.",
            "retry_decision": "change_plan",
            "memory_worthy": True,
            "confidence": 0.9,
        },
    )

    critique = parse_self_critique_json(raw)

    assert critique.trigger == "max_hops_exit"
    assert critique.dimension_scores["plan_quality"] == 1.0
    assert critique.dimension_scores["evidence"] == 0.0
    assert critique.dimension_scores["safety"] == 0.0
    assert critique.retry_decision == "change_plan"
    assert critique.memory_worthy is True


def test_memory_policy_gates_confidence_and_cooldown() -> None:
    critique = parse_self_critique_json(
        json.dumps(
            {
                "trigger": "failed_turn",
                "diagnosis": "The agent retried the same bad tool call.",
                "dimension_scores": {},
                "lesson": "When a tool fails twice, change plan before retrying.",
                "retry_decision": "change_plan",
                "memory_worthy": True,
                "confidence": 0.8,
            },
        ),
    )
    policy = SelfCritiqueMemoryPolicy(
        min_confidence=0.7,
        cooldown_seconds=60,
    )

    first = policy.candidate(critique, session_id="s1", now=1000)
    second = policy.candidate(critique, session_id="s1", now=1020)
    third = policy.candidate(critique, session_id="s1", now=1100)

    assert first is not None
    assert first.metadata["source"] == "self_critique"
    assert second is None
    assert third is not None


def test_memory_policy_rejects_non_memory_worthy_or_short_lessons() -> None:
    policy = SelfCritiqueMemoryPolicy(min_confidence=0.1, min_lesson_chars=10)
    not_worthy = parse_self_critique_json(
        json.dumps(
            {
                "trigger": "failed_turn",
                "diagnosis": "x",
                "dimension_scores": {},
                "lesson": "This is long enough.",
                "retry_decision": "retry",
                "memory_worthy": False,
                "confidence": 0.9,
            },
        ),
    )
    too_short = parse_self_critique_json(
        json.dumps(
            {
                "trigger": "failed_turn",
                "diagnosis": "x",
                "dimension_scores": {},
                "lesson": "short",
                "retry_decision": "retry",
                "memory_worthy": True,
                "confidence": 0.9,
            },
        ),
    )

    assert policy.candidate(not_worthy) is None
    assert policy.candidate(too_short) is None


def test_memory_policy_caps_materializations_per_session() -> None:
    policy = SelfCritiqueMemoryPolicy(
        min_confidence=0.1,
        cooldown_seconds=0,
        max_per_session=2,
    )

    def _critique(i: int):
        return parse_self_critique_json(
            json.dumps(
                {
                    "trigger": "failed_turn",
                    "diagnosis": f"Failure mode {i}.",
                    "dimension_scores": {},
                    "lesson": (
                        f"When failure mode {i} appears, switch strategy "
                        "before retrying the same action."
                    ),
                    "retry_decision": "change_plan",
                    "memory_worthy": True,
                    "confidence": 0.9,
                },
            ),
        )

    first = policy.candidate(_critique(1), session_id="sess", now=1000)
    second = policy.candidate(_critique(2), session_id="sess", now=1001)
    third = policy.candidate(_critique(3), session_id="sess", now=1002)
    other_session = policy.candidate(_critique(4), session_id="other", now=1003)

    assert first is not None
    assert second is not None
    assert second.metadata["session_write_count"] == 2
    assert second.metadata["session_write_limit"] == 2
    assert third is None
    assert other_session is not None


@pytest.mark.asyncio
async def test_materializer_writes_policy_approved_critique_to_long_term_memory() -> None:
    memory = _FakeMemoryService()
    materializer = SelfCritiqueMaterializer(
        policy=SelfCritiqueMemoryPolicy(min_confidence=0.5, cooldown_seconds=60),
    )

    result = await materializer.materialize(
        _memory_worthy_critique(),
        memory_service=memory,
        session_id="s1",
        now=1000,
    )

    assert result.status == "written"
    assert result.fact_id == "lesson:project:abc"
    assert len(memory.calls) == 1
    text, kwargs = memory.calls[0]
    assert "When trigger=tool_error" in text
    assert kwargs["kind"] == "lesson"
    assert kwargs["scope"] == "project"
    assert kwargs["layer"] == "long_term"
    assert kwargs["bucket"] == "failure_modes"
    assert kwargs["provenance"] == "self_critique"


@pytest.mark.asyncio
async def test_materializer_skips_when_policy_rejects_or_memory_missing() -> None:
    materializer = SelfCritiqueMaterializer(
        policy=SelfCritiqueMemoryPolicy(min_confidence=0.95),
    )

    rejected = await materializer.materialize(
        _memory_worthy_critique(),
        memory_service=_FakeMemoryService(),
        session_id="s1",
        now=1000,
    )
    missing = await materializer.materialize(
        _memory_worthy_critique(),
        memory_service=None,
        session_id="s1",
        now=1000,
    )

    assert rejected.status == "skipped"
    assert rejected.reason == "policy_rejected"
    assert missing.status == "skipped"
    assert missing.reason == "memory_service_missing"


@pytest.mark.asyncio
async def test_engine_runs_critic_and_materializes_memory_candidate() -> None:
    memory = _FakeMemoryService()
    seen_prompts = []

    async def _critic(prompt: str) -> str:
        seen_prompts.append(prompt)
        return json.dumps(
            {
                "trigger": "tool_error",
                "diagnosis": "The agent repeated a failing tool call.",
                "dimension_scores": {"tool_choice": 0.2},
                "lesson": (
                    "When a tool repeats the same failure, inspect state "
                    "or choose a different tool before retrying."
                ),
                "retry_decision": "change_plan",
                "memory_worthy": True,
                "confidence": 0.9,
            },
        )

    engine = SelfCritiqueEngine(
        materializer=SelfCritiqueMaterializer(
            policy=SelfCritiqueMemoryPolicy(
                min_confidence=0.5,
                cooldown_seconds=60,
            ),
        ),
    )
    result = await engine.run(
        SelfCritiqueRequest(
            trigger="tool_error",
            session_id="s-engine",
            goal="repair a file",
            failure_summary="apply_patch returned old_text not found",
            trajectory=(
                TrajectoryEvent(
                    kind="tool_call",
                    tool_name="apply_patch",
                    ok=False,
                    error="old_text not found",
                    content="patch stale text",
                ),
            ),
        ),
        critic_call=_critic,
        memory_service=memory,
        now=1000,
    )

    assert result.status == "completed"
    assert result.critique is not None
    assert result.critique.retry_decision == "change_plan"
    assert result.materialization is not None
    assert result.materialization.status == "written"
    assert seen_prompts and "Reflexion critic" in seen_prompts[0]
    assert len(memory.calls) == 1


@pytest.mark.asyncio
async def test_engine_skips_when_critic_missing() -> None:
    result = await SelfCritiqueEngine().run(
        SelfCritiqueRequest(trigger="failed_turn"),
        critic_call=None,
    )

    assert result.status == "skipped"
    assert result.error == "critic_call_missing"
