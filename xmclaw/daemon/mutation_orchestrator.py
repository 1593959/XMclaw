"""B-172: drive :class:`SkillMutator` from real grader signal.

Pre-B-172 the evolution pipeline produced **net-new** skills
(SkillProposer → ProposalMaterializer) but never **iterated** an
existing one. A skill that drifted into low-score territory just sat
there forever — the SkillMutator class existed (Epic #24 Phase 3
DSPy/GEPA wrapper) but nothing called it.

This orchestrator closes that gap. The signal chain:

  GRADER_VERDICT events → per-(skill_id, version) EWMA score →
    threshold check + cooldown gate → build dataset from events.db
    → run SkillMutator.mutate(...) → if candidate beats baseline:
      write versions/v<N+1>.md to disk + register v<N+1> in memory
      with set_head=False + emit SKILL_CANDIDATE_PROPOSED with
      decision="promote" + evidence=[score deltas]

Then ``EvolutionOrchestrator`` (existing) listens to that event and,
when ``evolution.auto_apply=true``, calls ``registry.promote(...)``
which atomically flips HEAD. ``set_head=False`` on register means a
worse-than-baseline mutation is registered (for audit) but never
becomes live.

Why per-(skill_id, version) EWMA, not per-skill: a freshly promoted
v2 should get its own learning curve, not inherit v1's score history.

Why an explicit cooldown: DSPy/GEPA is expensive (10s of seconds and
LLM tokens). Repeatedly mutating the same skill on every dip during
a noisy 10-call run would be wasteful.

Why default ON when DSPy might be missing: ``SkillMutator.mutate``
returns ``ok=False, reason="dspy_not_installed"`` gracefully — the
orchestrator just no-ops at the cost of one cheap function call per
trigger. Better than failing closed and silently never iterating.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType, make_event
from xmclaw.core.evolution.dataset import build_dataset_from_history
from xmclaw.core.evolution.mutator import SkillMutator
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry, UnknownSkillError
from xmclaw.utils.paths import default_events_db_path, user_skills_dir

_log = logging.getLogger(__name__)


@dataclass
class _SkillStats:
    """Per-skill running stats. EWMA + sample count + last-trigger ts."""

    ewma: float = 0.0
    samples: int = 0
    last_trigger_ts: float = 0.0
    in_flight: bool = False  # short-circuit re-entry while mutate is awaiting


@dataclass(frozen=True, slots=True)
class MutationDecision:
    """One mutation cycle's outcome — for tests + telemetry."""

    skill_id: str
    new_version: int
    baseline_score: float
    candidate_score: float
    promoted: bool   # whether we registered v<N+1> + emitted event
    reason: str = ""


class MutationOrchestrator:
    """Listen to grader signal, fire :class:`SkillMutator` when a
    skill underperforms, register the candidate, and emit a promote
    event for the existing :class:`EvolutionOrchestrator` to consume.

    Parameters
    ----------
    registry
        The shared ``SkillRegistry``.
    bus
        Shared event bus (subscribe + publish).
    skills_root
        Where ``<skill_id>/versions/v<N>.md`` archives go. Defaults
        to ``user_skills_dir()``.
    events_db_path
        Path to events.db for ``build_dataset_from_history``. Defaults
        to ``default_events_db_path()``.
    mutator
        Pre-constructed mutator. Tests inject a stub; production
        builds the real DSPy-backed one (gracefully no-ops when DSPy
        isn't installed).
    ewma_alpha
        Weight on the new sample. ``0.2`` ≈ EMA of last 5-10 calls,
        same shape EvolutionAgent observer uses.
    threshold
        EWMA value below which a skill is "underperforming". 0.5
        default — anything noticeably worse than coin-flip.
    min_samples
        Don't trigger before we've seen at least this many verdicts
        (prevents premature mutation on a fresh skill).
    cooldown_s
        Min seconds between successive mutation attempts on the same
        skill. 1 hour default.
    score_delta
        Candidate must beat baseline by at least this margin on
        holdout. Avoids churning on noise-level improvements.
    enabled
        Off-switch for tests / users on slow boxes who don't want the
        DSPy import overhead.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        bus: InProcessEventBus,
        *,
        skills_root: Path | None = None,
        events_db_path: Path | None = None,
        mutator: SkillMutator | None = None,
        ewma_alpha: float = 0.2,
        threshold: float = 0.5,
        min_samples: int = 5,
        cooldown_s: float = 3600.0,
        score_delta: float = 0.05,
        enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._skills_root = (
            skills_root if skills_root is not None else user_skills_dir()
        )
        self._events_db_path = (
            events_db_path
            if events_db_path is not None
            else default_events_db_path()
        )
        self._mutator = mutator if mutator is not None else SkillMutator()
        self._ewma_alpha = max(0.0, min(1.0, float(ewma_alpha)))
        self._threshold = float(threshold)
        self._min_samples = max(1, int(min_samples))
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._score_delta = max(0.0, float(score_delta))
        self._enabled = bool(enabled)
        self._subscription = None
        self._stats: dict[tuple[str, int], _SkillStats] = {}
        self._decisions: list[MutationDecision] = []  # for tests/telemetry

    # ── observability ───────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._subscription is not None

    @property
    def decisions(self) -> list[MutationDecision]:
        return list(self._decisions)

    def stats_for(
        self, skill_id: str, version: int = 1,
    ) -> _SkillStats | None:
        return self._stats.get((skill_id, version))

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to the bus. Idempotent. No-op when disabled."""
        if not self._enabled:
            return
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe(
            self._predicate, self._on_event,
        )
        _log.info(
            "mutation_orchestrator.start threshold=%.2f min_samples=%d "
            "cooldown_s=%.0f mutator_available=%s",
            self._threshold, self._min_samples, self._cooldown_s,
            self._mutator.is_available,
        )

    async def stop(self) -> None:
        """Unsubscribe. Idempotent."""
        sub = self._subscription
        self._subscription = None
        if sub is not None:
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001
                pass

    # ── event handler ───────────────────────────────────────────────

    def _predicate(self, event: BehavioralEvent) -> bool:
        return event.type is EventType.GRADER_VERDICT

    async def _on_event(self, event: BehavioralEvent) -> None:
        payload = event.payload or {}
        skill_id = payload.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            return
        version = int(payload.get("version", 1) or 1)
        score_raw = payload.get("score")
        if not isinstance(score_raw, (int, float)):
            return
        score = float(score_raw)

        key = (skill_id, version)
        stats = self._stats.setdefault(key, _SkillStats())
        # Standard EWMA update.
        if stats.samples == 0:
            stats.ewma = score
        else:
            stats.ewma = (
                self._ewma_alpha * score
                + (1.0 - self._ewma_alpha) * stats.ewma
            )
        stats.samples += 1

        if stats.in_flight:
            return  # mutation already running for this (id, version)
        if stats.samples < self._min_samples:
            return
        if stats.ewma >= self._threshold:
            return

        now = time.time()
        if now - stats.last_trigger_ts < self._cooldown_s:
            return

        stats.in_flight = True
        try:
            await self._maybe_mutate(skill_id, version, stats)
        finally:
            stats.in_flight = False
            stats.last_trigger_ts = time.time()

    async def _maybe_mutate(
        self, skill_id: str, version: int, stats: _SkillStats,
    ) -> None:
        # Pull baseline from registry. If the version is gone (rare —
        # e.g. someone unregistered between trigger and call), bail.
        try:
            baseline = self._registry.get(skill_id, version)
        except UnknownSkillError:
            return
        baseline_body = getattr(baseline, "body", None)
        if not isinstance(baseline_body, str) or not baseline_body.strip():
            # Python skills (no body attr) aren't mutable via SKILL.md
            # rewrite. Skip silently — Epic #25 will add a different
            # mutation flow for those.
            return

        # Build dataset from events.db for THIS skill.
        try:
            dataset = build_dataset_from_history(
                events_db_path=self._events_db_path,
                skill_id=skill_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "mutation_orchestrator.dataset_failed skill_id=%s err=%s",
                skill_id, exc,
            )
            return

        try:
            result = await self._mutator.mutate(
                skill_id=skill_id,
                baseline_text=baseline_body,
                dataset=dataset,
            )
        except Exception as exc:  # noqa: BLE001 — defend the loop
            _log.warning(
                "mutation_orchestrator.mutate_raised skill_id=%s err=%s",
                skill_id, exc,
            )
            return

        if not result.ok or result.candidate_text is None:
            self._decisions.append(MutationDecision(
                skill_id=skill_id,
                new_version=version + 1,
                baseline_score=result.baseline_score,
                candidate_score=result.candidate_holdout_score,
                promoted=False,
                reason=result.reason or "mutator_no_candidate",
            ))
            return

        if (
            result.candidate_holdout_score
            < result.baseline_score + self._score_delta
        ):
            self._decisions.append(MutationDecision(
                skill_id=skill_id,
                new_version=version + 1,
                baseline_score=result.baseline_score,
                candidate_score=result.candidate_holdout_score,
                promoted=False,
                reason=(
                    f"score_delta_below_threshold: "
                    f"{result.candidate_holdout_score:.3f} - "
                    f"{result.baseline_score:.3f} < {self._score_delta}"
                ),
            ))
            return

        # Found a real improvement — materialise it.
        new_version = self._next_version(skill_id, version)
        await self._materialise(
            skill_id=skill_id,
            new_version=new_version,
            candidate_text=result.candidate_text,
            baseline_score=result.baseline_score,
            candidate_score=result.candidate_holdout_score,
        )
        self._decisions.append(MutationDecision(
            skill_id=skill_id,
            new_version=new_version,
            baseline_score=result.baseline_score,
            candidate_score=result.candidate_holdout_score,
            promoted=True,
            reason="promoted",
        ))

    def _next_version(
        self, skill_id: str, current_version: int,
    ) -> int:
        """Return ``max(known_versions) + 1`` so mutation always
        produces a fresh number even if the registry already has v2/v3."""
        try:
            existing = self._registry.list_versions(skill_id)
        except Exception:  # noqa: BLE001
            existing = []
        if existing:
            return max(max(existing), current_version) + 1
        return current_version + 1

    async def _materialise(
        self,
        *,
        skill_id: str,
        new_version: int,
        candidate_text: str,
        baseline_score: float,
        candidate_score: float,
    ) -> None:
        # Write versions/v<N>.md so the skill survives daemon restart.
        # user_loader's B-172 extension reads this dir on next boot.
        skill_dir = self._skills_root / skill_id
        versions_dir = skill_dir / "versions"
        try:
            versions_dir.mkdir(parents=True, exist_ok=True)
            target = versions_dir / f"v{new_version}.md"
            target.write_text(candidate_text, encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "mutation_orchestrator.write_failed "
                "skill_id=%s version=%d err=%s",
                skill_id, new_version, exc,
            )
            return

        # Register in memory with set_head=False — HEAD stays on
        # current version until promote() flips it.
        skill = MarkdownProcedureSkill(
            id=skill_id, body=candidate_text, version=new_version,
        )
        evidence = [
            f"baseline_holdout={baseline_score:.3f}",
            f"candidate_holdout={candidate_score:.3f}",
            f"delta=+{candidate_score - baseline_score:.3f}",
        ]
        manifest = SkillManifest(
            id=skill_id, version=new_version,
            created_by="evolved", evidence=tuple(evidence),
        )
        try:
            self._registry.register(skill, manifest, set_head=False)
        except ValueError as exc:
            _log.info(
                "mutation_orchestrator.register_skipped "
                "skill_id=%s version=%d reason=%s",
                skill_id, new_version, exc,
            )
            return

        # Emit SKILL_CANDIDATE_PROPOSED with decision="promote" so
        # EvolutionOrchestrator can act on it (auto_apply=true →
        # registry.promote; otherwise the event sits for human review
        # via xmclaw evolve approve).
        try:
            event = make_event(
                session_id="_system:mutation",
                agent_id="mutation-orchestrator",
                type=EventType.SKILL_CANDIDATE_PROPOSED,
                payload={
                    "decision": "promote",
                    "winner_candidate_id": skill_id,
                    "winner_version": new_version,
                    "evidence": evidence,
                    "reason": (
                        f"mutator beat baseline by "
                        f"{candidate_score - baseline_score:.3f}"
                    ),
                },
            )
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "mutation_orchestrator.emit_failed err=%s", exc,
            )
            return

        _log.info(
            "mutation_orchestrator.materialised "
            "skill_id=%s new_version=%d delta=+%.3f path=%s",
            skill_id, new_version,
            candidate_score - baseline_score, target,
        )
