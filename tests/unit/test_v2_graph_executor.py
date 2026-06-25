from __future__ import annotations

import asyncio

import pytest

from xmclaw.cognition.graph_executor import GraphExecutor
from xmclaw.cognition.graph_runtime import GraphState, apply_updates


def _state() -> GraphState:
    return apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {
            "subtasks": [
                {"id": "a", "status": "pending"},
                {"id": "b", "status": "pending", "dependencies": ["a"]},
            ],
            "node_policies": [
                {"id": "a", "timeout_s": 2, "max_retries": 0},
                {"id": "b", "timeout_s": 2, "max_retries": 0},
            ],
        },
    )


@pytest.mark.asyncio
async def test_graph_executor_runs_nodes_in_dependency_order() -> None:
    calls: list[str] = []

    async def handler(node, state):
        calls.append(node["id"])
        return {"messages": {"role": "tool", "content": node["id"]}}

    result = await GraphExecutor().run(
        _state(),
        {"a": handler, "b": handler},
    )
    snap = result.state.snapshot()

    assert result.ok is True
    assert calls == ["a", "b"]
    assert result.executed_ids == ("a", "b")
    assert snap["final"] == "completed"
    assert [m["content"] for m in snap["messages"]] == ["a", "b"]
    assert [s["status"] for s in snap["subtasks"]] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_graph_executor_retries_failed_node() -> None:
    attempts = 0

    async def flaky(_node, _state):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return {"metadata": {"recovered": True}}

    state = apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {
            "subtasks": [{"id": "a", "status": "pending"}],
            "node_policies": [{"id": "a", "max_retries": 1, "backoff_s": 0}],
        },
    )

    result = await GraphExecutor().run(state, {"a": flaky})

    assert result.ok is True
    assert attempts == 2
    assert result.state.metadata["recovered"] is True


@pytest.mark.asyncio
async def test_graph_executor_fails_on_timeout() -> None:
    async def slow(_node, _state):
        raise asyncio.TimeoutError()

    state = apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {
            "subtasks": [{"id": "a", "status": "pending"}],
            "node_policies": [{"id": "a", "timeout_s": 1, "max_retries": 0}],
        },
    )

    result = await GraphExecutor().run(state, {"a": slow})

    assert result.ok is False
    assert result.failed_ids == ("a",)
    assert result.state.final == "failed"
    assert result.state.errors[0]["kind"] == "node_failed"
    assert "timeout" in result.state.errors[0]["message"]


@pytest.mark.asyncio
async def test_graph_executor_uses_cache_key() -> None:
    calls = 0

    async def handler(_node, _state):
        nonlocal calls
        calls += 1
        return {"metadata": {"value": "fresh"}}

    cache = {"k": {"metadata": {"value": "cached"}}}
    state = apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {
            "subtasks": [{"id": "a", "status": "pending"}],
            "node_policies": [{"id": "a", "cache_key": "k"}],
        },
    )

    result = await GraphExecutor(cache=cache).run(state, {"a": handler})

    assert calls == 0
    assert result.cached_ids == ("a",)
    assert result.state.metadata["value"] == "cached"
    assert result.state.subtasks[0]["cached"] is True


@pytest.mark.asyncio
async def test_graph_executor_fails_missing_handler() -> None:
    result = await GraphExecutor().run(
        apply_updates(
            GraphState(thread_id="t", run_id="r"),
            {"subtasks": [{"id": "a", "status": "pending"}]},
        ),
        {},
    )

    assert result.ok is False
    assert result.failed_ids == ("a",)
    assert result.state.errors[0]["kind"] == "missing_handler"


@pytest.mark.asyncio
async def test_graph_executor_stops_when_node_returns_pending() -> None:
    calls = 0

    async def handler(node, _state):
        nonlocal calls
        calls += 1
        return {
            "subtasks": {
                "id": node["id"],
                "status": "pending",
                "reason": "waiting for percept",
            },
        }

    state = apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {"subtasks": [{"id": "a", "status": "pending"}]},
    )

    result = await GraphExecutor().run(state, {"a": handler})

    assert calls == 1
    assert result.ok is False
    assert result.blocked_ids == ("a",)
    assert result.state.final == "pending"
    assert result.state.subtasks[0]["status"] == "pending"
    assert result.state.subtasks[0]["reason"] == "waiting for percept"


@pytest.mark.asyncio
async def test_graph_executor_stops_when_node_returns_blocked() -> None:
    async def handler(node, _state):
        return {"subtasks": {"id": node["id"], "status": "blocked"}}

    state = apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {"subtasks": [{"id": "a", "status": "pending"}]},
    )

    result = await GraphExecutor().run(state, {"a": handler})

    assert result.ok is False
    assert result.blocked_ids == ("a",)
    assert result.state.final == "pending"


@pytest.mark.asyncio
async def test_graph_executor_refuses_invalid_graph_before_execution() -> None:
    called = False

    async def handler(_node, _state):
        nonlocal called
        called = True
        return {}

    state = apply_updates(
        GraphState(thread_id="t", run_id="r"),
        {"subtasks": [{"id": "a", "status": "pending", "dependencies": ["missing"]}]},
    )

    result = await GraphExecutor().run(state, {"a": handler})

    assert called is False
    assert result.ok is False
    assert result.state.final == "failed"
