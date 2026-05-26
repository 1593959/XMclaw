"""Process-wide counter for swallowed exceptions.

Background
==========

The codebase has ~1200 ``except Exception:  # noqa: BLE001`` clauses.
Each one logs a warning and continues. Most are correct defensive
code (a single bad blob shouldn't take down the daemon), but some
hide real bugs — the 2026-05-25 chat-image upload silently failed
for weeks because a missing ``time`` import threw ``NameError``
inside one of these swallows. The only signal was a daemon-log
"image save failed: %s" warning that nobody grepped.

This module provides:

* :func:`record` — call from inside the ``except`` clause with
  ``(scope, exc)``. Increments an in-process counter keyed on
  ``(scope, exc_class_name)``.
* :func:`snapshot` — returns the current counters for the doctor
  check / observability endpoint.
* :func:`reset` — used by tests + after a daemon restart.

Doctor surfaces the snapshot so ``xmclaw doctor`` can flag any
``(scope, exc_class)`` that fired > N times in the current daemon
uptime — pointing the operator at the swallow before it becomes a
silent multi-week failure mode.

Design notes
------------

In-process only by design: persisting to disk would just shift the
"who reads it" problem one level. The doctor check + Web UI panel
read the live counters; a daemon restart legitimately wipes them
(the operator already saw the prior run's state).

The counter is a plain ``defaultdict(int)`` guarded by a thread
lock — increments must be cheap (sub-microsecond) so that callers
inside their hot-path except clauses don't pay any meaningful
overhead.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


_lock = threading.Lock()
_counts: defaultdict[tuple[str, str], int] = defaultdict(int)


def record(scope: str, exc: BaseException) -> None:
    """Count one swallowed exception.

    ``scope`` is a short stable identifier — usually the module +
    function the swallow lives in, e.g. ``"ws_image_intake.save"``.
    Keep it stable across releases so dashboards / alerts can pin
    a specific swallow over time.

    Fast path: no I/O, no string formatting. Cheap enough that
    every defensive ``except`` clause can call it without measurable
    overhead.
    """
    cls_name = type(exc).__name__
    with _lock:
        _counts[(scope, cls_name)] += 1


def snapshot() -> dict[str, int]:
    """Return a copy of the current counters as a flat dict.

    Keys are ``"<scope>:<exc_class>"`` so the result is JSON-
    serialisable for the doctor check + status endpoint.
    """
    with _lock:
        return {f"{scope}:{cls}": n for (scope, cls), n in _counts.items()}


def total() -> int:
    """Sum of every swallow recorded since boot / reset."""
    with _lock:
        return sum(_counts.values())


def reset() -> None:
    """Clear all counters. Used by tests + post-restart hooks."""
    with _lock:
        _counts.clear()


def hottest(limit: int = 10) -> list[tuple[str, str, int]]:
    """Return the top-``limit`` (scope, exc_class, count) tuples.

    The doctor check pulls this to surface the worst offenders;
    operators usually only need the top of the list.
    """
    with _lock:
        items = sorted(
            ((s, c, n) for (s, c), n in _counts.items()),
            key=lambda t: t[2], reverse=True,
        )
    return items[:limit]


__all__ = ["record", "snapshot", "total", "reset", "hottest"]
