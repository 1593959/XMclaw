"""B-295: per-skill UCB1 variant selector.

Phase 3.1 designed self-evolution as:
  ``proposer → candidate vN+1 → tested vs HEAD → if better, promote``

But there was no "tested" step in production: ``SkillToolProvider.invoke``
always grabs ``registry.get(skill_id)`` (HEAD only), so candidate
versions never get traffic, never accumulate plays, never qualify for
the controller's ``min_plays`` threshold, never get promoted. The
chain ran but produced zero promotions because the variant-tasting
stage was missing.

This module fills that gap. ``VariantSelector`` subscribes to
``GRADER_VERDICT`` events, maintains per-(skill_id, version) UCB1
stats, and exposes ``pick_version(skill_id)`` for ``SkillToolProvider``
to call before each invocation. With it wired:

  * HEAD plays accumulate (it's the highest-mean arm at start)
  * Candidates get occasional explore plays driven by UCB1
  * After enough plays + good scores, EvolutionAgent.evaluate (B-294)
    sees a candidate clear the controller thresholds and proposes a
    promotion

Design notes:
  * Pure UCB1 — no Thompson sampling / contextual bandit. Phase 1
    keeps it simple; richer policies plug in via the selector
    interface later.
  * HEAD warm-up: for the first ``head_warmup_plays`` calls per skill,
    we always return HEAD even if a candidate's UCB score is higher.
    Avoids paying with bad UX while the policy is learning.
  * Optional bus subscription: if no bus is provided the selector
    works in "manual" mode (caller calls ``record_outcome`` directly).
    Used by tests + bench harness.
  * Failure isolation: any exception inside the selector falls back
    to HEAD. A bad selector MUST NOT break the user's tool call.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _ArmStats:
    plays: int = 0
    total_reward: float = 0.0

    @property
    def mean(self) -> float:
        return self.total_reward / self.plays if self.plays else 0.0


@dataclass
class VariantSelector:
    """Per-skill UCB1 variant selector.

    Construction does NOT subscribe — call ``start(bus)`` to begin
    consuming GRADER_VERDICT events, or call ``record_outcome``
    manually (tests / bench).

    Args:
        registry: SkillRegistry for ``list_versions`` + ``active_version``.
        exploration_c: UCB1 exploration constant. Default 2.0.
        head_warmup_plays: per-skill plays before bandit kicks in.
            Default 5 — enough to seed HEAD's mean before exploring.
        prior_mean: assumed mean for arms with 0 plays. Default 0.5
            (neutral). Higher → more explore early.
    """

    registry: Any  # SkillRegistry — Any to avoid import cycle
    exploration_c: float = 2.0
    head_warmup_plays: int = 5
    prior_mean: float = 0.5

    _stats: dict[tuple[str, int], _ArmStats] = field(default_factory=dict)
    _subscription: Any = None
    _enabled: bool = True

    # ── public ──────────────────────────────────────────────────────

    def pick_version(self, skill_id: str) -> int | None:
        """Pick which version of ``skill_id`` to run this turn.

        Returns:
            * The chosen version int.
            * ``None`` if the skill has no HEAD set (caller should
              treat as "skill not promoted yet" and bail).
            * Always HEAD when the selector is disabled or the skill
              has only one registered version.
        """
        if not self._enabled:
            return self._safe_active(skill_id)

        try:
            head = self._safe_active(skill_id)
            if head is None:
                return None
            versions = self._safe_versions(skill_id)
            if len(versions) <= 1:
                return head

            # HEAD warm-up: feed the bandit some HEAD plays first so the
            # comparison has a meaningful baseline.
            head_stats = self._stats.get((skill_id, head))
            head_plays = head_stats.plays if head_stats else 0
            if head_plays < self.head_warmup_plays:
                return head

            # UCB1 over all known versions.
            total_plays = sum(
                self._stats.get((skill_id, v), _ArmStats()).plays
                for v in versions
            )
            best_v = head
            best_score = -float("inf")
            log_total = math.log(max(total_plays, 1))
            for v in versions:
                arm = self._stats.get((skill_id, v))
                if arm is None or arm.plays == 0:
                    # Unexplored arm → infinite UCB. Pick it.
                    return v
                conf = self.exploration_c * math.sqrt(log_total / arm.plays)
                score = arm.mean + conf
                if score > best_score:
                    best_score = score
                    best_v = v
            return best_v
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "variant_selector.pick_failed skill=%s err=%s",
                skill_id, exc,
            )
            # Fail-safe: HEAD.
            return self._safe_active(skill_id)

    def record_outcome(
        self, skill_id: str, version: int, score: float,
    ) -> None:
        """Update per-arm stats. Called by ``_on_verdict`` (subscription
        path) or manually (tests / bench)."""
        key = (skill_id, int(version))
        arm = self._stats.get(key)
        if arm is None:
            arm = _ArmStats()
            self._stats[key] = arm
        arm.plays += 1
        arm.total_reward += float(score)

    @property
    def is_active(self) -> bool:
        return self._subscription is not None

    @property
    def stats_snapshot(self) -> dict[tuple[str, int], dict]:
        """Read-only snapshot for diagnostics / Evolution page."""
        return {
            k: {"plays": v.plays, "mean": v.mean}
            for k, v in self._stats.items()
        }

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self, bus: Any) -> None:
        """Subscribe to GRADER_VERDICT. Idempotent."""
        if self._subscription is not None:
            return
        from xmclaw.core.bus import EventType
        self._subscription = bus.subscribe(
            lambda e: e.type == EventType.GRADER_VERDICT,
            self._on_verdict,
        )
        logger.info(
            "variant_selector.start exploration_c=%.2f warmup=%d",
            self.exploration_c, self.head_warmup_plays,
        )

    async def stop(self) -> None:
        sub = self._subscription
        self._subscription = None
        if sub is not None:
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001
                pass

    def disable(self) -> None:
        """Force HEAD-only mode. Useful for emergencies (sticky bug
        attribute to a candidate, want to peg to HEAD without unwiring
        the subscription)."""
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    # ── internals ───────────────────────────────────────────────────

    async def _on_verdict(self, event: Any) -> None:
        try:
            payload = event.payload or {}
            skill_id = payload.get("skill_id")
            score = payload.get("score")
            if skill_id is None or score is None:
                return
            version = int(payload.get("version", 0))
            self.record_outcome(str(skill_id), version, float(score))
        except Exception as exc:  # noqa: BLE001
            logger.warning("variant_selector.ingest_failed err=%s", exc)

    def _safe_active(self, skill_id: str) -> int | None:
        try:
            return self.registry.active_version(skill_id)
        except Exception:  # noqa: BLE001
            return None

    def _safe_versions(self, skill_id: str) -> list[int]:
        try:
            return list(self.registry.list_versions(skill_id))
        except Exception:  # noqa: BLE001
            return []


__all__ = ["VariantSelector"]
