from __future__ import annotations

import pytest

from xmclaw.cognition.graph_runtime import (
    GraphState,
    NodePolicy,
    ReducerRegistry,
    apply_updates,
    inspect_graph_state,
    with_policy,
)


def test_graph_state_reducers_append_and_merge_without_mutation() -> None:
    state = GraphState(thread_id="s1", run_id="r1", goal="ship it")
    updated = apply_updates(state, {
        "messages": [{"role": "user", "content": "hi"}],
        "tool_results": {"name": "bash", "ok": True},
        "metadata": {"mode": "agent"},
        "confidence": 0.4,
    })
    updated2 = apply_updates(updated, {
        "messages": [{"role": "assistant", "content": "ok"}],
        "metadata": {"phase": "reasoning"},
        "confidence": 0.2,
    })

    assert state.messages == ()
    assert [m["role"] for m in updated2.messages] == ["user", "assistant"]
    assert updated2.tool_results == ({"name": "bash", "ok": True},)
    assert updated2.metadata == {"mode": "agent", "phase": "reasoning"}
    assert updated2.confidence == 0.4


def test_subtask_reducer_merges_lifecycle_updates_by_step_id() -> None:
    state = GraphState(thread_id="s1", run_id="r1")
    declared = apply_updates(state, {
        "subtasks": [{"id": "step_a", "index": 0, "status": "pending"}],
    })
    completed = apply_updates(declared, {
        "subtasks": {
            "step_id": "step_a",
            "step_index": 0,
            "status": "completed",
            "output_keys": ["text"],
        },
    })

    assert len(completed.subtasks) == 1
    assert completed.subtasks[0]["id"] == "step_a"
    assert completed.subtasks[0]["step_id"] == "step_a"
    assert completed.subtasks[0]["status"] == "completed"
    assert completed.subtasks[0]["output_keys"] == ["text"]


def test_graph_state_unknown_keys_land_in_metadata() -> None:
    state = GraphState(thread_id="s1", run_id="r1")
    updated = apply_updates(state, {"planner_score": 0.8})
    assert updated.metadata["planner_score"] == 0.8


def test_graph_state_immutable_identity_fields() -> None:
    state = GraphState(thread_id="s1", run_id="r1")
    with pytest.raises(ValueError, match="thread_id"):
        apply_updates(state, {"thread_id": "s2"})


def test_graph_state_snapshot_roundtrip() -> None:
    state = apply_updates(
        GraphState(thread_id="s1", run_id="r1"),
        {
            "subtasks": [{"id": "a", "title": "A"}],
            "node_policies": [{"id": "a", "timeout_s": 10.0}],
            "errors": [{"node": "tool", "error": "boom"}],
            "final": "done",
        },
    )
    restored = GraphState.from_snapshot(state.snapshot())
    assert restored == state


def test_custom_reducer_can_override_key_behavior() -> None:
    reg = ReducerRegistry({"confidence": lambda cur, upd: float(upd)})
    state = GraphState(thread_id="s1", run_id="r1", confidence=0.9)
    updated = reg.apply(state, {"confidence": 0.1})
    assert updated.confidence == 0.1


def test_node_policy_normalizes_bounds_and_dicts() -> None:
    assert NodePolicy(timeout_s=-1, max_retries=-2, backoff_s=-3).normalized() == NodePolicy(
        timeout_s=1.0,
        max_retries=0,
        backoff_s=0.0,
    )
    policy = with_policy({
        "timeout_s": 0,
        "max_retries": 3,
        "backoff_s": 2,
        "cache_key": "",
        "error_handler": "retry_then_handoff",
    })
    assert policy.timeout_s == 1.0
    assert policy.max_retries == 3
    assert policy.backoff_s == 2.0
    assert policy.cache_key is None
    assert policy.error_handler == "retry_then_handoff"


def test_inspect_graph_state_reports_runnable_and_blocked_nodes() -> None:
    state = apply_updates(
        GraphState(thread_id="s1", run_id="r1"),
        {
            "subtasks": [
                {"id": "a", "status": "completed"},
                {"id": "b", "status": "pending", "dependencies": ["a"]},
                {"id": "c", "status": "pending", "dependencies": ["b"]},
            ],
            "node_policies": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        },
    )

    inspection = inspect_graph_state(state)

    assert inspection.ok is True
    assert inspection.runnable_ids == ("b",)
    assert inspection.blocked_ids == ("c",)
    assert inspection.failed_ids == ()
    assert inspection.policy_missing == ()


def test_inspect_graph_state_reports_missing_dependencies_and_failures() -> None:
    state = apply_updates(
        GraphState(thread_id="s1", run_id="r1"),
        {
            "subtasks": [
                {"id": "a", "status": "pending", "dependencies": ["missing"]},
                {"id": "b", "status": "escalated"},
            ],
            "node_policies": [{"id": "a"}],
        },
    )

    inspection = inspect_graph_state(state)

    assert inspection.ok is False
    assert inspection.missing_dependencies == ({"id": "a", "dependency": "missing"},)
    assert inspection.failed_ids == ("b",)
    assert inspection.policy_missing == ("b",)
    assert inspection.to_dict()["failed_ids"] == ["b"]


def test_inspect_graph_state_detects_cycles() -> None:
    state = apply_updates(
        GraphState(thread_id="s1", run_id="r1"),
        {
            "subtasks": [
                {"id": "a", "status": "pending", "dependencies": ["b"]},
                {"id": "b", "status": "pending", "dependencies": ["a"]},
            ],
            "node_policies": [{"id": "a"}, {"id": "b"}],
        },
    )

    inspection = inspect_graph_state(state)

    assert inspection.ok is False
    assert inspection.cycles == (("a", "b", "a"),)
