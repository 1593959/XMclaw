"""Cognition router route-order regression.

Pre-fix bug: ``/api/v2/cognition/tasks/graph`` returned 404 because
``/tasks/{task_id}`` was registered BEFORE ``/tasks/graph`` in the
file, so FastAPI matched ``/tasks/graph`` to the parameterized route
with ``task_id="graph"``, looked up the (non-existent) task, and
returned 404 — masking the existence of the DAG endpoint entirely.

This test pins the registration order: every concrete sub-route
under a parameterized prefix MUST appear before the parameterized
route. Specific to the ``/tasks/*`` family today; can be extended
to ``/graph/*`` if similar collisions are added later.
"""
from __future__ import annotations

from xmclaw.daemon.routers.cognition import router


def _route_order(path_prefix: str) -> list[str]:
    """Return the ordered list of route paths under ``path_prefix``
    (e.g. "/api/v2/cognition/tasks") in the order they were registered."""
    out: list[str] = []
    for r in router.routes:
        path = getattr(r, "path", "")
        if path.startswith(path_prefix):
            out.append(path)
    return out


def test_cognition_router_tasks_graph_before_task_id() -> None:
    """``/tasks/graph`` MUST be registered before ``/tasks/{task_id}``
    so the DAG endpoint isn't masked by the parameterized route.
    """
    paths = _route_order("/api/v2/cognition/tasks")
    assert "/api/v2/cognition/tasks/graph" in paths, (
        "/tasks/graph route missing entirely from cognition router"
    )
    assert "/api/v2/cognition/tasks/{task_id}" in paths, "/tasks/{task_id} route missing"
    graph_idx = paths.index("/api/v2/cognition/tasks/graph")
    param_idx = paths.index("/api/v2/cognition/tasks/{task_id}")
    assert graph_idx < param_idx, (
        f"Route order bug: ``/tasks/graph`` (idx {graph_idx}) is "
        f"registered AFTER ``/tasks/{{task_id}}`` (idx {param_idx}). "
        f"FastAPI matches in registration order — concrete routes "
        f"must precede parameterized siblings.\n"
        f"Full /tasks order: {paths}"
    )


def test_cognition_router_concrete_subpaths_before_param() -> None:
    """General invariant: any concrete sub-route under a
    parameterized prefix must come first. Currently only checks the
    ``/tasks/*`` family — extend if new collisions appear.
    """
    families = [
        ("/api/v2/cognition/tasks", "/api/v2/cognition/tasks/{task_id}"),
        # Future-proof slot: when /goals/<concrete> is added it must
        # come before /goals/{goal_id} (currently only /goals exists).
    ]
    for prefix, param_route in families:
        paths = _route_order(prefix)
        if param_route not in paths:
            continue
        param_idx = paths.index(param_route)
        # Any path that is /<prefix>/<concrete> (no curly braces)
        # registered AFTER param_idx is a bug.
        for i, p in enumerate(paths):
            if i <= param_idx:
                continue
            if p.startswith(prefix + "/") and "{" not in p[len(prefix) + 1:]:
                raise AssertionError(
                    f"Concrete route {p!r} (idx {i}) is registered "
                    f"AFTER parameterized {param_route!r} (idx "
                    f"{param_idx}). Move {p!r} ABOVE the parameterized "
                    f"route."
                )
