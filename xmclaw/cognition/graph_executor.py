"""Lightweight GraphState executor.

The executor is intentionally small and framework-free: it runs node
handlers against a ``GraphState`` using dependency ordering and the
serializable ``node_policies`` already stored in the state.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from xmclaw.cognition.graph_runtime import (
    GraphInspection,
    GraphState,
    apply_updates,
    inspect_graph_state,
    with_policy,
)


NodeHandler = Callable[[dict[str, Any], GraphState], Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True, slots=True)
class GraphExecutionResult:
    state: GraphState
    inspection: GraphInspection
    executed_ids: tuple[str, ...] = ()
    cached_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    blocked_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state.final == "completed" and self.inspection.ok and not self.failed_ids


class GraphExecutor:
    """Execute GraphState subtasks with policy-aware retries."""

    def __init__(
        self,
        *,
        cache: dict[str, Mapping[str, Any]] | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self.cache = cache if cache is not None else {}
        self.max_concurrency = max(1, int(max_concurrency))

    async def run(
        self,
        state: GraphState,
        handlers: Mapping[str, NodeHandler],
    ) -> GraphExecutionResult:
        inspection = inspect_graph_state(state)
        if inspection.missing_dependencies or inspection.cycles:
            failed_state = apply_updates(state, {
                "final": "failed",
                "metadata": {"inspection": inspection.to_dict()},
            })
            return GraphExecutionResult(
                state=failed_state,
                inspection=inspection,
            )

        current = state
        executed: list[str] = []
        cached: list[str] = []
        failed: list[str] = []
        blocked: list[str] = []

        while True:
            runnable = _runnable_nodes(current)
            if not runnable:
                break
            batch = runnable[:self.max_concurrency]
            results = await asyncio.gather(
                *[self._run_one(current, node, handlers) for node in batch],
            )
            for node_id, updates, status in results:
                if status == "cached":
                    cached.append(node_id)
                elif status == "executed":
                    executed.append(node_id)
                elif status == "failed":
                    failed.append(node_id)
                elif status == "blocked":
                    blocked.append(node_id)
                current = apply_updates(current, updates)
            if failed or blocked:
                break

        final_inspection = inspect_graph_state(current)
        if failed or not final_inspection.ok:
            final = "failed"
        elif blocked or final_inspection.runnable_ids or final_inspection.blocked_ids:
            final = "pending"
        else:
            final = "completed"
        current = apply_updates(current, {
            "final": final,
            "metadata": {"inspection": final_inspection.to_dict()},
        })
        return GraphExecutionResult(
            state=current,
            inspection=final_inspection,
            executed_ids=tuple(executed),
            cached_ids=tuple(cached),
            failed_ids=tuple(failed),
            blocked_ids=tuple(blocked),
        )

    async def _run_one(
        self,
        state: GraphState,
        node: dict[str, Any],
        handlers: Mapping[str, NodeHandler],
    ) -> tuple[str, dict[str, Any], str]:
        node_id = _node_id(node)
        policy = _policy_for(state, node_id)
        cache_key = policy.cache_key
        if cache_key and cache_key in self.cache:
            return node_id, _success_updates(
                node_id,
                dict(self.cache[cache_key]),
                cached=True,
            ), "cached"

        handler = handlers.get(node_id) or handlers.get(str(node.get("kind") or ""))
        if handler is None:
            return node_id, _failure_updates(
                node_id,
                "missing_handler",
                "no handler registered for graph node",
            ), "failed"

        attempts = policy.max_retries + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                raw_updates = await asyncio.wait_for(
                    handler(node, state),
                    timeout=policy.timeout_s,
                )
                updates = dict(raw_updates or {})
                node_status = _node_status_from_updates(updates, node_id)
                if node_status in {"pending", "blocked", "waiting"}:
                    return node_id, _blocked_updates(
                        node_id,
                        updates,
                        node_status,
                    ), "blocked"
                if cache_key:
                    self.cache[cache_key] = dict(updates)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                return node_id, _success_updates(
                    node_id,
                    updates,
                    elapsed_ms=elapsed_ms,
                ), "executed"
            except asyncio.TimeoutError:
                last_error = f"timeout after {policy.timeout_s:.1f}s"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts and policy.backoff_s > 0:
                await asyncio.sleep(policy.backoff_s)

        return node_id, _failure_updates(
            node_id,
            "node_failed",
            last_error or "node failed",
        ), "failed"


def _runnable_nodes(state: GraphState) -> list[dict[str, Any]]:
    completed = {
        _node_id(dict(node))
        for node in state.subtasks
        if isinstance(node, Mapping)
        and str(node.get("status") or "").lower() == "completed"
    }
    out: list[dict[str, Any]] = []
    for raw in state.subtasks:
        if not isinstance(raw, Mapping):
            continue
        node = dict(raw)
        status = str(node.get("status") or "pending").lower()
        if status not in {"pending", "retrying", "blocked"}:
            continue
        deps = _dependencies(node)
        if all(dep in completed for dep in deps):
            out.append(node)
    return out


def _policy_for(state: GraphState, node_id: str):
    for raw in state.node_policies:
        if not isinstance(raw, Mapping):
            continue
        policy_id = str(raw.get("id") or raw.get("step_id") or raw.get("index"))
        if policy_id == node_id:
            return with_policy(raw)
    return with_policy(None)


def _node_id(node: Mapping[str, Any]) -> str:
    return str(node.get("id") or node.get("step_id") or node.get("index"))


def _dependencies(node: Mapping[str, Any]) -> tuple[str, ...]:
    raw = node.get("dependencies", node.get("depends_on"))
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, list | tuple | set):
        return tuple(str(v) for v in raw if str(v))
    return ()


def _success_updates(
    node_id: str,
    updates: Mapping[str, Any],
    *,
    elapsed_ms: float = 0.0,
    cached: bool = False,
) -> dict[str, Any]:
    merged = dict(updates)
    merged.setdefault("subtasks", {
        "id": node_id,
        "status": "completed",
        "cached": cached,
        "latency_ms": elapsed_ms,
    })
    return merged


def _blocked_updates(
    node_id: str,
    updates: Mapping[str, Any],
    status: str,
) -> dict[str, Any]:
    merged = dict(updates)
    merged.setdefault("subtasks", {"id": node_id, "status": status})
    return merged


def _failure_updates(node_id: str, kind: str, message: str) -> dict[str, Any]:
    return {
        "subtasks": {"id": node_id, "status": "failed", "error": message},
        "errors": {"kind": kind, "node_id": node_id, "message": message},
    }


def _node_status_from_updates(
    updates: Mapping[str, Any],
    node_id: str,
) -> str:
    raw = updates.get("subtasks")
    items = raw if isinstance(raw, list | tuple) else [raw]
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or item.get("step_id") or item.get("index"))
        if item_id == node_id:
            return str(item.get("status") or "").lower()
    return ""


__all__ = [
    "GraphExecutionResult",
    "GraphExecutor",
    "NodeHandler",
]
