"""Epic #27 sweep #10 (2026-05-19) — HoldoutTestSignal registry.

Pre-fix, ``HoldoutTestSignal.probe`` was a stub that always returned
``None`` (signal not applicable) unless the test passed an explicit
``holdout_test_passed`` payload override. That made the docs claim
("multi-signal grading honestly composed") larger than the reality
(only ``UserFollowupSignal`` actually fired in production), which
the sweep audit caught.

This module is the load-bearing piece the docstring referenced. It
gives production callers a way to register deterministic holdout
checks against ``eval_test_id`` strings, which ``HoldoutTestSignal``
then resolves and runs at probe time.

Usage pattern (production)::

    from xmclaw.core.grader.holdout_registry import register

    def _post_invoke_verify(payload: dict) -> bool:
        return Path(payload.get("output_path", "")).exists()

    register("file-write-creates-output", _post_invoke_verify)

Usage pattern (testing)::

    # Tests can pass the override directly through the event payload
    # (``holdout_test_passed: bool``) without touching this registry.

Resolution order at probe time:
    1. Payload ``holdout_test_passed`` override (test escape hatch;
       preserved exactly as-was for backward compat with
       :mod:`tests.unit.test_v2_signals_holdout_cross`).
    2. Registry lookup on ``eval_test_id`` → registered callable →
       run with ``event.payload``.
    3. None (signal not applicable) on miss / exception.

Defensive design:
    * Registrations are name-spaced by string — callers SHOULD use
      a dotted prefix (``"xmclaw.skill_xyz.verify_v1"``) to avoid
      collisions. We don't enforce this; we DO log a warning when a
      registration overwrites an existing entry.
    * Callables MUST be synchronous (or return a coroutine the
      signal will await — both forms are accepted). We catch every
      exception so a buggy verify hook can never crash the grader.
    * Module is process-local. Registry is lost on daemon restart;
      callers re-register from boot-time hooks (typically
      :mod:`xmclaw.daemon.app_lifespan`). This is intentional — a
      persisted registry would invite "stale callable pointing to
      deleted skill" bugs that are worse than the boot-time
      re-registration cost.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

log = logging.getLogger(__name__)


# A holdout check signature: takes the event payload (read-only),
# returns either a bool (sync) or an awaitable resolving to bool.
HoldoutCheck = Callable[
    [Mapping[str, Any]],
    Union[bool, Awaitable[bool]],
]


_REGISTRY: dict[str, HoldoutCheck] = {}


def register(eval_test_id: str, check: HoldoutCheck) -> None:
    """Register a deterministic post-state check under ``eval_test_id``.

    Re-registering the same id replaces the previous callable and
    emits a WARNING — the typical legitimate cause is hot-reload
    after editing a skill's verify hook, but it can also be a
    name-collision bug worth surfacing.
    """
    if not isinstance(eval_test_id, str) or not eval_test_id.strip():
        raise ValueError(
            "eval_test_id must be a non-empty string"
        )
    if not callable(check):
        raise ValueError(
            f"check must be callable, got {type(check).__name__}"
        )
    key = eval_test_id.strip()
    if key in _REGISTRY:
        log.warning(
            "holdout_registry.overwriting eval_test_id=%s", key,
        )
    _REGISTRY[key] = check


def unregister(eval_test_id: str) -> bool:
    """Remove a registration. Returns True if a row was removed,
    False otherwise. Idempotent: removing an unknown id is fine."""
    return _REGISTRY.pop(eval_test_id.strip(), None) is not None


def lookup(eval_test_id: str) -> Optional[HoldoutCheck]:
    """Return the registered callable for ``eval_test_id``, or
    ``None`` if no registration exists."""
    if not isinstance(eval_test_id, str):
        return None
    return _REGISTRY.get(eval_test_id.strip())


def clear() -> None:
    """Drop all registrations. Test-only hook — production code
    should never call this (loses every skill's verify hook)."""
    _REGISTRY.clear()


def registered_ids() -> list[str]:
    """Return a snapshot of all registered ids (for diagnostics /
    ``skill_status`` style introspection). Order is insertion."""
    return list(_REGISTRY.keys())


async def run_check(
    eval_test_id: str,
    payload: Mapping[str, Any],
) -> Optional[bool]:
    """Resolve + run a registered check. Returns:
        * True / False — the check ran and produced a verdict.
        * None — no registration OR the callable raised; signal
          should treat as "not applicable" (no negative score).

    Accepts both sync + async callables. Catches every exception
    so a buggy hook never crashes the grader.
    """
    fn = lookup(eval_test_id)
    if fn is None:
        return None
    try:
        out = fn(payload)
        if inspect.isawaitable(out):
            out = await out
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "holdout_registry.check_failed eval_test_id=%s "
            "exc=%s: %s",
            eval_test_id, type(exc).__name__, exc,
        )
        return None
    return bool(out) if isinstance(out, (bool, int)) else None


__all__ = [
    "HoldoutCheck",
    "clear",
    "lookup",
    "register",
    "registered_ids",
    "run_check",
    "unregister",
]
