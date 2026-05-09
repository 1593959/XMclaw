"""Per-context Pareto frontier — multiple HEAD candidates per skill.

Background
----------
Single-winner promotion (the legacy ``EvolutionController`` shape)
collapses every (skill_id, task family) signal into one score and
picks one HEAD. That throws away the GEPA paper's central insight:
different versions of the same skill can be Pareto-optimal on
different *contexts* (task families), and forcing a single global
winner loses the wins.

This module owns a per-(skill_id, context_signature) winner set. It is
**state only** — no scheduling, no promotion logic, no bus events. The
controller / orchestrator wires reads/writes; we just hold the
frontier and answer "what's the best version for this context?".

Public API
----------
* :class:`FrontierEntry` — frozen dataclass; one entry on the frontier.
* :class:`ParetoFrontier`:
    - ``add(entry)`` — insert if non-dominated; returns ``True``/``False``.
    - ``select_for(skill_id, context)`` — best version for the context;
      falls back to the global best across contexts when no
      context-specific entry exists.
    - ``all_for(skill_id)`` — every entry on the skill's frontier.
    - ``evict_dominated()`` — periodic compaction; returns count.

Design notes
------------
* **Domination rule.** Within a single ``(skill_id, context_signature)``
  bucket, entry ``a`` dominates ``b`` iff ``a.grader_score > b.grader_score``
  AND ``a.evidence_count >= b.evidence_count`` (more evidence + better
  score). Equality on both axes is *not* domination — we keep both,
  letting ``max_per_context`` evict the older one when capacity is hit.
* **No domination across contexts.** Two entries with different
  ``context_signature`` values never dominate each other; that's the
  whole point of the per-context frontier.
* **Cap by capacity.** When ``add`` would exceed ``max_per_context`` for
  a bucket, the lowest-scoring incumbent is evicted (ties broken by
  oldest ``added_at``). The new entry only displaces if it would not
  itself be the loser.
* **Determinism.** Same input sequence → same frontier. Storage is
  list-based with explicit tie-breakers; no dict ordering games.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrontierEntry:
    """One entry on the per-context Pareto frontier.

    Attributes:
        skill_id: target skill.
        version: integer skill version (increments on each promotion).
        context_signature: task-family bucket. ``"default"`` is the
            catch-all when no specialisation exists.
        grader_score: the live HonestGrader's score, in ``[0, 1]``.
        evidence_count: number of grader verdicts behind ``grader_score``.
            Higher = more confident the score is real.
        added_at: ``time.time()`` when this entry was admitted.
    """
    skill_id: str
    version: int
    context_signature: str
    grader_score: float
    evidence_count: int
    added_at: float


def _dominates(a: FrontierEntry, b: FrontierEntry) -> bool:
    """``a`` Pareto-dominates ``b`` within the same context bucket.

    Both axes must be at-least-as-good and at least one strictly
    better. We define the axes as ``grader_score`` (higher better) and
    ``evidence_count`` (higher better). See module docstring.
    """
    if a.skill_id != b.skill_id:
        return False
    if a.context_signature != b.context_signature:
        return False
    score_ge = a.grader_score >= b.grader_score
    evid_ge = a.evidence_count >= b.evidence_count
    score_gt = a.grader_score > b.grader_score
    evid_gt = a.evidence_count > b.evidence_count
    return score_ge and evid_ge and (score_gt or evid_gt)


class ParetoFrontier:
    """Per-(skill_id, context_signature) winner set.

    NOT a global winner. Two entries with different
    ``context_signature`` values coexist freely.

    Args:
        max_per_context: hard cap on entries per ``(skill, context)``
            bucket. When exceeded, the lowest-scoring incumbent is
            evicted (ties broken by oldest ``added_at``).
    """

    def __init__(self, max_per_context: int = 3) -> None:
        if max_per_context < 1:
            raise ValueError("max_per_context must be >= 1")
        self._max_per_context = max_per_context
        # Bucketed by (skill_id, context_signature); within a bucket,
        # entries are stored insertion-order and we filter on read.
        self._buckets: dict[tuple[str, str], list[FrontierEntry]] = {}

    # ── mutation ─────────────────────────────────────────────────────

    def add(self, e: FrontierEntry) -> bool:
        """Admit ``e`` if it's not dominated by an existing entry.

        Returns ``True`` if the entry was admitted, ``False`` if some
        existing entry in the same ``(skill, context)`` bucket already
        dominates it. Existing entries that ``e`` dominates are removed
        before admission.

        When the bucket already holds ``max_per_context`` entries and
        ``e`` survives the domination check, the lowest-scoring incumbent
        (oldest on ties) is evicted unless ``e`` itself would be that
        loser — in which case ``e`` is rejected.
        """
        key = (e.skill_id, e.context_signature)
        bucket = self._buckets.setdefault(key, [])

        # 1. Reject if some incumbent dominates ``e``.
        for incumbent in bucket:
            if _dominates(incumbent, e):
                return False

        # 2. Drop any incumbent ``e`` itself dominates.
        bucket[:] = [x for x in bucket if not _dominates(e, x)]

        # 3. Capacity check — evict the worst loser if needed.
        if len(bucket) >= self._max_per_context:
            # Combine candidates: existing bucket + ``e``. Sort by
            # (score asc, added_at asc); evict the head unless it == e,
            # in which case ``e`` is the loser and we reject.
            combined = list(bucket) + [e]
            combined.sort(key=lambda x: (x.grader_score, x.added_at))
            loser = combined[0]
            if loser is e:
                return False
            bucket[:] = [x for x in bucket if x is not loser]

        bucket.append(e)
        return True

    def evict_dominated(self) -> int:
        """Recompute every bucket, dropping any entry dominated by another.

        Useful after a batch insertion that bypassed the per-add
        domination check (e.g., a bulk reload from persistence). Returns
        the number of entries removed.

        Within a bucket, an entry survives iff no *other* entry in the
        same bucket strictly dominates it.
        """
        removed = 0
        for key, bucket in list(self._buckets.items()):
            survivors: list[FrontierEntry] = []
            for entry in bucket:
                if any(
                    _dominates(other, entry)
                    for other in bucket
                    if other is not entry
                ):
                    removed += 1
                    continue
                survivors.append(entry)
            if survivors:
                self._buckets[key] = survivors
            else:
                # All entries dropped (degenerate; shouldn't happen
                # because no entry dominates itself, but be safe).
                self._buckets.pop(key, None)
        return removed

    # ── read ─────────────────────────────────────────────────────────

    def select_for(
        self, skill_id: str, context_signature: str
    ) -> FrontierEntry | None:
        """Best version for ``(skill_id, context_signature)``.

        Resolution order:

        1. Best entry in the exact ``(skill, context)`` bucket if
           non-empty.
        2. Otherwise, best entry across *all* contexts for this skill
           — the "global fallback" the GEPA paper relies on when a
           novel context shows up.
        3. ``None`` if the skill has no frontier at all.

        "Best" within a candidate set is defined as max ``grader_score``,
        ties broken by max ``evidence_count``, then earliest ``added_at``
        (deterministic).
        """
        bucket = self._buckets.get((skill_id, context_signature))
        if bucket:
            return _best(bucket)
        # Global fallback: union across all contexts for this skill.
        all_skill_entries = [
            entry
            for (sid, _ctx), entries in self._buckets.items()
            if sid == skill_id
            for entry in entries
        ]
        if not all_skill_entries:
            return None
        return _best(all_skill_entries)

    def all_for(self, skill_id: str) -> list[FrontierEntry]:
        """Every frontier entry for a skill across all contexts.

        Order is deterministic: sorted by ``(context_signature,
        version, added_at)``. Returns an empty list when the skill has
        no entries.
        """
        out: list[FrontierEntry] = []
        for (sid, _ctx), entries in self._buckets.items():
            if sid == skill_id:
                out.extend(entries)
        out.sort(
            key=lambda e: (e.context_signature, e.version, e.added_at)
        )
        return out


def _best(entries: Iterable[FrontierEntry]) -> FrontierEntry | None:
    """Pick the deterministically-best entry from a non-empty iterable.

    Tie-breakers: ``grader_score`` desc, ``evidence_count`` desc,
    ``added_at`` asc (earliest wins). Returns ``None`` only when the
    iterable yields nothing.
    """
    best: FrontierEntry | None = None
    for e in entries:
        if best is None:
            best = e
            continue
        if (e.grader_score, e.evidence_count, -e.added_at) > (
            best.grader_score,
            best.evidence_count,
            -best.added_at,
        ):
            best = e
    return best
