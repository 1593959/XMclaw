"""Small graph-state runtime primitives for XMclaw agents.

This module is intentionally dependency-free. It borrows the useful
shape of LangGraph's runtime -- state keys, reducers, node policies,
and checkpoint-friendly snapshots -- without forcing the daemon to
adopt LangGraph as a hard dependency.

The first integration target is narrow: give Reasoning, Planning,
Tool Use, Memory, reflection, and sub-agent fanout one shared state
contract. Existing AgentLoop / Planner / ActionDispatcher code can
adopt this module gradually.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping


Reducer = Callable[[Any, Any], Any]


def append_list(current: Any, update: Any) -> list[Any]:
    """Reducer that appends list-like updates without mutating inputs."""
    left = list(current or [])
    if update is None:
        return left
    if isinstance(update, list | tuple):
        return left + list(update)
    return left + [update]


def merge_list_by_identity(current: Any, update: Any) -> list[Any]:
    """Reducer for lists of dicts keyed by id, step_id, or index.

    Used for subtask state where updates should refine the existing
    entry instead of accumulating duplicate lifecycle rows.
    """
    out = list(current or [])
    updates = list(update) if isinstance(update, list | tuple) else [update]
    for item in updates:
        if not isinstance(item, Mapping):
            out.append(item)
            continue
        key = _identity_key(item)
        if key is None:
            out.append(dict(item))
            continue
        replaced = False
        for idx, existing in enumerate(out):
            if isinstance(existing, Mapping) and _identity_key(existing) == key:
                merged = dict(existing)
                merged.update(dict(item))
                out[idx] = merged
                replaced = True
                break
        if not replaced:
            out.append(dict(item))
    return out


def _identity_key(item: Mapping[str, Any]) -> tuple[str, Any] | None:
    for field in ("id", "step_id"):
        value = item.get(field)
        if value is not None and value != "":
            return "id", value
    if "index" in item:
        return "index", item.get("index")
    if "step_index" in item:
        return "index", item.get("step_index")
    return None


def merge_dict(current: Any, update: Any) -> dict[str, Any]:
    """Reducer that shallow-merges mapping updates."""
    out = dict(current or {})
    if update is None:
        return out
    if not isinstance(update, Mapping):
        raise TypeError(f"merge_dict expected mapping update, got {type(update).__name__}")
    out.update(dict(update))
    return out


def max_float(current: Any, update: Any) -> float:
    """Reducer that keeps the maximum numeric value."""
    if update is None:
        return float(current or 0.0)
    return max(float(current or 0.0), float(update))


def last_value(_current: Any, update: Any) -> Any:
    """Reducer that replaces the current value."""
    return update


@dataclass(frozen=True, slots=True)
class NodePolicy:
    """Execution policy for one graph node.

    `cache_key` and `error_handler` are names, not callables, so this
    object is serializable and can be stored in checkpoints.
    """

    timeout_s: float = 300.0
    max_retries: int = 2
    backoff_s: float = 1.0
    cache_key: str | None = None
    error_handler: str | None = None

    def normalized(self) -> "NodePolicy":
        return NodePolicy(
            timeout_s=max(1.0, float(self.timeout_s)),
            max_retries=max(0, int(self.max_retries)),
            backoff_s=max(0.0, float(self.backoff_s)),
            cache_key=self.cache_key or None,
            error_handler=self.error_handler or None,
        )


@dataclass(frozen=True, slots=True)
class GraphState:
    """Checkpoint-friendly state envelope for agent graph execution."""

    thread_id: str
    run_id: str
    goal: str = ""
    messages: tuple[dict[str, Any], ...] = ()
    subtasks: tuple[dict[str, Any], ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()
    memory_hits: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    node_policies: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0
    final: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable checkpoint snapshot."""
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "goal": self.goal,
            "messages": list(self.messages),
            "subtasks": list(self.subtasks),
            "tool_results": list(self.tool_results),
            "memory_hits": list(self.memory_hits),
            "artifacts": list(self.artifacts),
            "errors": list(self.errors),
            "node_policies": list(self.node_policies),
            "confidence": self.confidence,
            "final": self.final,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "GraphState":
        """Rehydrate state from a snapshot produced by `snapshot`."""
        return cls(
            thread_id=str(data.get("thread_id") or ""),
            run_id=str(data.get("run_id") or ""),
            goal=str(data.get("goal") or ""),
            messages=tuple(data.get("messages") or ()),
            subtasks=tuple(data.get("subtasks") or ()),
            tool_results=tuple(data.get("tool_results") or ()),
            memory_hits=tuple(data.get("memory_hits") or ()),
            artifacts=tuple(data.get("artifacts") or ()),
            errors=tuple(data.get("errors") or ()),
            node_policies=tuple(data.get("node_policies") or ()),
            confidence=float(data.get("confidence") or 0.0),
            final=str(data.get("final") or ""),
            metadata=dict(data.get("metadata") or {}),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


@dataclass(frozen=True, slots=True)
class GraphInspection:
    """Static health summary for a GraphState DAG."""

    ok: bool
    runnable_ids: tuple[str, ...] = ()
    blocked_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    missing_dependencies: tuple[dict[str, Any], ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    policy_missing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "runnable_ids": list(self.runnable_ids),
            "blocked_ids": list(self.blocked_ids),
            "failed_ids": list(self.failed_ids),
            "missing_dependencies": list(self.missing_dependencies),
            "cycles": [list(c) for c in self.cycles],
            "policy_missing": list(self.policy_missing),
        }


class ReducerRegistry:
    """Apply state updates through per-key reducers."""

    def __init__(self, reducers: Mapping[str, Reducer] | None = None) -> None:
        self._reducers: dict[str, Reducer] = dict(DEFAULT_REDUCERS)
        if reducers:
            self._reducers.update(dict(reducers))

    def reducer_for(self, key: str) -> Reducer:
        return self._reducers.get(key, last_value)

    def apply(self, state: GraphState, updates: Mapping[str, Any]) -> GraphState:
        values = state.snapshot()
        for key, update in updates.items():
            if key in {"thread_id", "run_id", "created_at"}:
                raise ValueError(f"{key} is immutable in GraphState updates")
            if key not in values:
                values.setdefault("metadata", {})
                values["metadata"] = merge_dict(values["metadata"], {key: update})
                continue
            values[key] = self.reducer_for(key)(values.get(key), update)
        values["updated_at"] = time.time()
        return GraphState.from_snapshot(values)


DEFAULT_REDUCERS: dict[str, Reducer] = {
    "messages": append_list,
    "subtasks": merge_list_by_identity,
    "tool_results": append_list,
    "memory_hits": append_list,
    "artifacts": append_list,
    "errors": append_list,
    "node_policies": merge_list_by_identity,
    "confidence": max_float,
    "metadata": merge_dict,
    "goal": last_value,
    "final": last_value,
}


def apply_updates(state: GraphState, updates: Mapping[str, Any]) -> GraphState:
    """Convenience wrapper using the default reducer registry."""
    return ReducerRegistry().apply(state, updates)


def with_policy(policy: NodePolicy | Mapping[str, Any] | None) -> NodePolicy:
    """Normalize a dict or NodePolicy into a serializable policy object."""
    if policy is None:
        return NodePolicy()
    if isinstance(policy, NodePolicy):
        return policy.normalized()
    return NodePolicy(
        timeout_s=float(policy.get("timeout_s", 300.0)),
        max_retries=int(policy.get("max_retries", 2)),
        backoff_s=float(policy.get("backoff_s", 1.0)),
        cache_key=policy.get("cache_key"),
        error_handler=policy.get("error_handler"),
    ).normalized()


def inspect_graph_state(state: GraphState) -> GraphInspection:
    """Inspect task topology, dependencies, failure, and policy coverage."""
    nodes = {
        str(item.get("id") or item.get("step_id") or item.get("index")): dict(item)
        for item in state.subtasks
        if isinstance(item, Mapping)
    }
    policies = {
        str(item.get("id") or item.get("step_id") or item.get("index"))
        for item in state.node_policies
        if isinstance(item, Mapping)
    }
    missing: list[dict[str, Any]] = []
    runnable: list[str] = []
    blocked: list[str] = []
    failed: list[str] = []
    completed = {
        node_id
        for node_id, node in nodes.items()
        if str(node.get("status") or "").lower() == "completed"
    }
    terminal_fail = {"failed", "escalated", "error"}

    for node_id, node in nodes.items():
        status = str(node.get("status") or "pending").lower()
        deps = _dependency_ids(node)
        for dep_id in deps:
            if dep_id not in nodes:
                missing.append({"id": node_id, "dependency": dep_id})
        if status in terminal_fail:
            failed.append(node_id)
        elif deps and any(dep not in completed for dep in deps):
            blocked.append(node_id)
        elif status in {"pending", "retrying", "blocked"}:
            runnable.append(node_id)

    cycles = _find_cycles({node_id: _dependency_ids(node) for node_id, node in nodes.items()})
    policy_missing = tuple(sorted(node_id for node_id in nodes if node_id not in policies))
    ok = not missing and not cycles and not failed
    return GraphInspection(
        ok=ok,
        runnable_ids=tuple(runnable),
        blocked_ids=tuple(blocked),
        failed_ids=tuple(failed),
        missing_dependencies=tuple(missing),
        cycles=tuple(cycles),
        policy_missing=policy_missing,
    )


def _dependency_ids(node: Mapping[str, Any]) -> tuple[str, ...]:
    raw = node.get("dependencies")
    if raw is None:
        raw = node.get("depends_on")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, list | tuple | set):
        return tuple(str(v) for v in raw if str(v))
    return ()


def _find_cycles(graph: Mapping[str, tuple[str, ...]]) -> list[tuple[str, ...]]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    cycles: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        if node_id in visiting:
            try:
                idx = stack.index(node_id)
            except ValueError:
                idx = 0
            cycle = tuple(stack[idx:] + [node_id])
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if node_id in visited:
            return
        visiting.add(node_id)
        stack.append(node_id)
        for dep_id in graph.get(node_id, ()):
            if dep_id in graph:
                visit(dep_id)
        stack.pop()
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in graph:
        visit(node_id)
    return cycles


__all__ = [
    "DEFAULT_REDUCERS",
    "GraphState",
    "GraphInspection",
    "NodePolicy",
    "Reducer",
    "ReducerRegistry",
    "append_list",
    "apply_updates",
    "last_value",
    "max_float",
    "merge_list_by_identity",
    "merge_dict",
    "inspect_graph_state",
    "with_policy",
]
