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
    """An entry in a skill's history log — one per promote/rollback event.

    B-121: ``source`` distinguishes the *origin* of the decision, not
    the content of the evidence. Audit consumers care about this when
    answering "did the controller decide this on its own, or did a
    human force it?":

      * ``"manual"`` — UI button, HTTP API, REPL command, test setup
        (default — explicit calls are manual unless flagged otherwise)
      * ``"controller"`` — :class:`EvolutionController` produced a
        ``PROMOTE`` / ``ROLLBACK`` decision and the orchestrator's
        proposal subscriber applied it
      * ``"system"`` — boot-time defaults, migrations, registry
        bootstrapping
    """

    kind: Literal["promote", "rollback"]
    skill_id: str
    from_version: int
    to_version: int
    ts: float
    evidence: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None  # usually set on rollback; optional for promote
    source: str = "manual"      # B-121: "manual" | "controller" | "system"


def now_ts() -> float:
    """Seam for tests; real code just calls time.time() via this helper."""
    return time.time()
