from __future__ import annotations

from xmclaw.context.summarizer_eviction import SummarizerEvictionPlanner
from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.base import Message


def _msg(role: str, content: str = "", **kw) -> Message:
    return Message(role=role, content=content, **kw)


def _tc(id: str = "call_1") -> ToolCall:
    return ToolCall(name="bash", args={"command": "pwd"}, id=id, provenance="test")


def test_eviction_plan_preserves_head_tail_and_user_messages() -> None:
    messages = [
        _msg("system", "sys"),
        _msg("user", "opening"),
        _msg("assistant", "old answer"),
        _msg("user", "middle ask"),
        _msg("assistant", "middle answer"),
        _msg("tool", "middle tool", tool_call_id="x"),
        _msg("user", "latest"),
    ]

    plan = SummarizerEvictionPlanner().plan(
        messages,
        session_id="s1",
        summarize_start=2,
        summarize_end=6,
    )

    assert plan.should_summarize is True
    assert plan.source_indices == (2, 3, 4, 5)
    assert plan.summarize_indices == (2, 4, 5)
    assert 3 in plan.preserved_indices
    assert 6 in plan.protected_indices
    assert plan.evict_ratio == 3 / len(messages)
    assert plan.provenance["range"] == [2, 6]


def test_eviction_plan_aligns_away_from_orphan_tool_start() -> None:
    messages = [
        _msg("system", "sys"),
        _msg("tool", "orphan", tool_call_id="old"),
        _msg("assistant", "answer"),
        _msg("user", "latest"),
    ]

    plan = SummarizerEvictionPlanner().plan(
        messages,
        summarize_start=1,
        summarize_end=3,
    )

    assert plan.summarize_start == 2
    assert plan.source_indices == (2,)
    assert plan.summarize_indices == (2,)
    assert 1 in plan.protected_indices


def test_eviction_plan_keeps_assistant_tool_pair_together_at_end() -> None:
    messages = [
        _msg("system", "sys"),
        _msg("assistant", "will call", tool_calls=(_tc("call_1"),)),
        _msg("tool", "tool out", tool_call_id="call_1"),
        _msg("user", "latest"),
    ]

    plan = SummarizerEvictionPlanner().plan(
        messages,
        summarize_start=1,
        summarize_end=2,
    )

    assert plan.summarize_end == 3
    assert plan.summarize_indices == (1, 2)
    assert plan.ranges[0].to_dict() == {
        "start": 1,
        "end": 3,
        "reason": "middle_context_over_budget",
    }


def test_eviction_plan_serializes_for_checkpointing() -> None:
    messages = [
        _msg("system", "sys"),
        _msg("assistant", "old"),
        _msg("user", "latest"),
    ]

    plan = SummarizerEvictionPlanner().plan(
        messages,
        session_id="s2",
        summarize_start=1,
        summarize_end=2,
        focus_topic="deploy",
        model_profile="balanced",
        created_at=1234.5,
    )

    data = plan.to_dict()
    assert data["session_id"] == "s2"
    assert data["ranges"] == [
        {"start": 1, "end": 2, "reason": "middle_context_over_budget"},
    ]
    assert data["provenance"]["focus_topic"] == "deploy"
    assert data["summary_provenance"] == {
        "source": "SummarizerEvictionPlanner",
        "session_id": "s2",
        "summary_kind": "conversation_middle",
        "source_message_range": [1, 2],
        "source_indices": [1],
        "summarize_indices": [1],
        "preserved_indices": [0, 2],
        "evict_ratio": 1 / 3,
        "model_profile": "balanced",
        "created_at": 1234.5,
        "focus_topic": "deploy",
    }
    assert data["provenance"]["summary_provenance"] == data["summary_provenance"]
