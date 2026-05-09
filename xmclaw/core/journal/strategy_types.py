"""Strategy dataclass + helpers — Sprint 3 #6 (ReasoningBank-style).

A :class:`Strategy` is a distilled "when X, then Y" pattern derived from
a window of session journals. It is the memory unit of the
ReasoningBank-style strategy distillation pipeline.

Design follows ``docs/EVOLUTION_HONEST_STATE.md`` Iron Rules:

* **Iron Rule #1 — Min 2 evidence**. A strategy must be supported by
  at least :data:`MIN_EVIDENCE_COUNT` (=2) session ids. One session is
  noise; two is the smallest signal we accept. Filtering happens both
  at distill-time (LLM is told to drop ambiguous patterns) and at
  ``StrategyBank.put`` time (defence in depth — never trust the LLM).

* **Iron Rule #2 — Confidence cap**. Even when every session in the
  window agrees, raw model confidence is bounded by
  :data:`CONFIDENCE_CAP` (=0.60). Strategies are *recall hints*, not
  policy. The grader still owns final call; over-confident hints have
  driven regressions in the past (see roadmap entry for the 2026-04
  rollback). The cap is applied in :func:`cap_confidence` and the
  bank/distiller both go through it.

A Strategy's ``id`` is a stable SHA256 of its (when_pattern,
then_action) tuple. This keeps :class:`StrategyBank.put` idempotent —
re-distilling the same pattern from a later journal window updates
``last_retrieved_at`` / ``evidence_count`` but does not multiply rows.

The dataclass is frozen + slotted so subscribers can treat it as a
value type and hash it into deduping sets without surprise.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Final


MIN_EVIDENCE_COUNT: Final[int] = 2
"""Iron Rule #1. Strategies with fewer than this many supporting
session ids must be dropped at every layer (distiller + bank)."""

CONFIDENCE_CAP: Final[float] = 0.60
"""Iron Rule #2. Maximum confidence a strategy is allowed to carry.
Both the distiller (post-LLM-parse) and any caller minting a Strategy
by hand should pass raw confidence through :func:`cap_confidence`."""


def make_strategy_id(when: str, then: str) -> str:
    """Stable id for a (when, then) pair.

    Uses SHA256 over ``f"{when}\\n--\\n{then}"`` so two strategies are
    merged iff their patterns match exactly. The ``--`` separator is
    deliberate: prevents the ambiguity where ``when="ab"`` ``then="c"``
    would otherwise hash the same as ``when="a"`` ``then="bc"``.

    Output is the full 64-char hex digest. We never truncate — the
    bank uses this as a primary key and any collision risk added by
    truncation isn't worth the few bytes saved.
    """
    payload = f"{when}\n--\n{then}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cap_confidence(raw: float) -> float:
    """Clamp ``raw`` into ``[0.0, CONFIDENCE_CAP]``.

    NaN / non-numeric input is rejected by float() coercion at the
    call site (we keep this function pure-numeric to make tests
    cheap). Negative values clamp to 0.0; values above the cap clamp
    to :data:`CONFIDENCE_CAP`.
    """
    val = float(raw)
    if val < 0.0:
        return 0.0
    if val > CONFIDENCE_CAP:
        return CONFIDENCE_CAP
    return val


@dataclass(frozen=True, slots=True)
class Strategy:
    """One distilled "when X, then Y" pattern.

    Construction is intentionally raw — :func:`cap_confidence` is the
    caller's responsibility (the distiller and the bank both apply
    it). Tests can build Strategy objects directly with any
    confidence value to verify enforcement at higher layers.

    Fields
    ------
    id:
        SHA256 over (when_pattern, then_action). Stable across calls.
    when_pattern:
        Free-text description of the situation the strategy applies
        to. The distiller is told to phrase this as a recognizable
        pattern, not a single transcript line.
    then_action:
        Free-text description of the recommended action. Should be
        actionable in a future turn; the bank surfaces it as a recall
        hint.
    evidence_count:
        Number of distinct session ids this pattern was observed in.
        Must be >= :data:`MIN_EVIDENCE_COUNT` for the strategy to be
        accepted by :class:`StrategyBank.put`.
    evidence_session_ids:
        The supporting session ids. Tuple (frozen + hashable) so
        Strategy itself stays hashable.
    confidence:
        Bounded score in ``[0.0, CONFIDENCE_CAP]``. Higher means the
        distiller saw the pattern more cleanly across the window.
    distilled_at:
        Wall-clock seconds when this Strategy was minted.
    last_retrieved_at:
        Wall-clock seconds of the most recent successful
        ``retrieve()`` hit. ``None`` until first retrieval. Used by
        :meth:`StrategyBank.prune_unused` so a freshly-minted but
        never-recalled strategy is eligible for pruning.
    """

    id: str
    when_pattern: str
    then_action: str
    evidence_count: int
    evidence_session_ids: tuple[str, ...]
    confidence: float
    distilled_at: float = field(default_factory=lambda: time.time())
    last_retrieved_at: float | None = None


__all__ = [
    "CONFIDENCE_CAP",
    "MIN_EVIDENCE_COUNT",
    "Strategy",
    "cap_confidence",
    "make_strategy_id",
]
