"""Skill versioning primitives — promotion, rollback, audit records.

Anti-req #5 (every skill is a versioned artifact, rollback is
first-class) and anti-req #12 (every promotion carries evidence, no
silent auto-promote) are encoded here as data structures that the
``SkillRegistry`` persists and replays.

A promotion record is forever. Even after rollback, the record stays
in the history — that's the audit trail the grader and the user need
in order to decide whether evolution did something useful.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    """An entry in a skill's history log — one per promote/rollback event."""

    kind: Literal["promote", "rollback"]
    skill_id: str
    from_version: int
    to_version: int
    ts: float
    evidence: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None  # usually set on rollback; optional for promote


def now_ts() -> float:
    """Seam for tests; real code just calls time.time() via this helper."""
    return time.time()
