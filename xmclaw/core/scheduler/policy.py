"""Tool / model selection policy.

Phase 1 uses a trivial round-robin + grader-weighted pick. Phase 4 upgrades
to learned policy from cross-session signal (anti-req: evolution-as-scheduler).
"""
from __future__ import annotations


def best_of_n(candidates: list, scores: list[float]) -> int:  # noqa: ARG001
    """Return index of highest-scored candidate. Phase 1 helper."""
    raise NotImplementedError("Phase 1")
