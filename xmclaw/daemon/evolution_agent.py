"""EvolutionAgent — headless observer workspace (Epic #17 Phase 7).

The evolution pipeline assembled under ``xmclaw/core/evolution/`` +
``xmclaw/core/grader/`` + ``xmclaw/core/scheduler/`` is a *decision* layer.
Nothing in the daemon runs it end-to-end — the closed loop from bench
data → promotion report exists only in the bench harness
(``tests/bench/phase3_autonomous_evolution_live.py``). Phase 7 brings the
loop into the running daemon, but not inside the main agent. A separate
"evolution" agent workspace subscribes to the bus, aggregates grader
verdicts per (skill_id, version), and periodically calls
:meth:`EvolutionController.consider_promotion`. When the controller
returns PROMOTE, the observer publishes a :data:`EventType.SKILL_CANDIDATE_PROPOSED`
event — it never touches the :class:`SkillRegistry` itself. Enforcement
of anti-req #12 ("no promotion without evidence") stays at the registry
door, where the main agent's turn loop is the one call site.

Why a separate workspace and not just a subscriber? Three reasons:

* **Independent lifecycle** — the observer can be stopped / restarted /
  reconfigured without touching the main agent's turn loop.
* **Independent config** — thresholds (``min_plays``, ``min_mean``,
  etc.) are per-observer, so two observers can coexist with different
  gate settings for A/B experiments.
* **Independent audit** — decisions are logged to
  ``<data>/v2/evolution/<agent_id>/decisions.jsonl`` under the observer's
  own ID. When the UI later surfaces "which agent proposed this?", the
  answer is a clean 1:1 mapping.

Audit log format (one JSON object per line):

.. code-block:: json

    {
      "ts": 1700000000.123,
      "agent_id": "evo-main",
      "decision": "promote" | "no_change" | "rollback",
      "head_version": 3,
      "winner_candidate_id": "summary.v4",
      "winner_version": 4,
      "evaluations": [
        {"candidate_id": "summary.v3", "version": 3, "plays": 12,
         "mean_score": 0.71},
        …
      ],
      "evidence": ["plays=15", "mean=0.79", …],
      "reason": "all gates cleared"
    }

Design constraints (from ``xmclaw/daemon/AGENTS.md``):

* Only imports from ``core/``, ``utils/``, ``security/``. Never from
  ``providers/`` — the observer does not need an LLM or tool stack.
* The observer runs asyncio-native; ``publish`` / ``subscribe`` calls on
  :class:`InProcessEventBus` are awaited directly.
* Handler exceptions are isolated by the bus itself; this class still
  wraps aggregation in try/except to avoid a bad event blowing up the
  subscription task, which would silently stop all further updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType, make_event
from xmclaw.core.bus.memory import Subscription
from xmclaw.core.evolution.controller import (
    CandidateEvaluation,
    EvolutionController,
    EvolutionDecision,
    EvolutionReport,
    PromotionThresholds,
)
from xmclaw.utils.paths import evolution_dir

log = logging.getLogger(__name__)


@dataclass
class _ArmAggregate:
    """Running sum for one (skill_id, version) pair.

    Mirrors :class:`xmclaw.core.scheduler.online.ArmStats` in spirit but
    keyed by (skill_id, version) rather than an index — the observer
    may learn about new candidates mid-run and can't assume a dense
    integer space.
    """

    skill_id: str
    version: int
    plays: int = 0
    total_reward: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def mean(self) -> float:
        return self.total_reward / self.plays if self.plays else 0.0


class EvolutionAgent:
    """Headless observer that proposes skill promotions.

    Not a FastAPI handler, not an AgentLoop. Subscribes to the bus
    on :meth:`start` and unsubscribes on :meth:`stop`. Does not serve
    WS turns — this is the "workspace kind" that Phase 7 introduces.

    Parameters
    ----------
    agent_id : str
        Stable ID the workspace is registered under. Used for the audit
        log subdirectory and the ``agent_id`` field on any events the
        observer publishes.
    bus : InProcessEventBus
        The shared event bus the main agent publishes to. The observer
        both reads (grader verdicts) and writes (candidate proposals)
        through this bus.
    thresholds : PromotionThresholds | None
        Promotion gate config. Defaults to the controller's built-ins.
    audit_dir : Path | None
        Override the audit subtree root. Tests pass ``tmp_path``; prod
        lets this fall back to :func:`xmclaw.utils.paths.evolution_dir`.
    """

    def __init__(
        self,
        agent_id: str,
        bus: InProcessEventBus,
        *,
        thresholds: PromotionThresholds | None = None,
        audit_dir: Path | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._bus = bus
        self._controller = EvolutionController(thresholds)
        base = audit_dir if audit_dir is not None else evolution_dir()
        self._audit_path = base / agent_id / "decisions.jsonl"
        self._arms: dict[tuple[str, int], _ArmAggregate] = {}
        self._lock = asyncio.Lock()
        self._subscription: Subscription | None = None

    # ── public lifecycle ─────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def audit_path(self) -> Path:
        return self._audit_path

    def is_running(self) -> bool:
        return self._subscription is not None

    async def start(self) -> None:
        """Subscribe to grader verdicts. Idempotent."""
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe(
            lambda e: e.type == EventType.GRADER_VERDICT,
            self._on_event,
        )
        log.info("evolution.start", extra={"agent_id": self._agent_id})

    async def stop(self) -> None:
        """Cancel the subscription. Idempotent.

        Does NOT clear :attr:`_arms` — a restart should keep the
        aggregate so a daemon bounce inside a long evolution session
        doesn't reset the counters. Tests that need a clean slate call
        :meth:`reset` explicitly.
        """
        if self._subscription is None:
            return
        self._subscription.cancel()
        self._subscription = None
        log.info("evolution.stop", extra={"agent_id": self._agent_id})

    # ── aggregation ──────────────────────────────────────────────────

    def snapshot(self) -> list[CandidateEvaluation]:
        """Build the controller's input from the running aggregate.

        Pure over the current ``_arms`` map — no locking needed because
        the caller (``evaluate``) holds the lock that serializes writes.
        Kept as a method (not a property) so the cost is visible at the
        call site; Phase 8's UI will poll this for the "candidates"
        panel.
        """
        return [
            CandidateEvaluation(
                candidate_id=arm.skill_id,
                version=arm.version,
                plays=arm.plays,
                mean_score=arm.mean,
                notes=dict(arm.notes),
            )
            for arm in self._arms.values()
        ]

    async def _on_event(self, event: BehavioralEvent) -> None:
        """Bus callback. Filters + updates the per-arm aggregate.

        Wrapped in try/except beyond what the bus does on its own: the
        bus isolates handler exceptions per event, but a bad payload
        parse here would log-and-skip without the aggregate being
        corrupted mid-update.
        """
        try:
            await self._ingest(event)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "evolution.ingest_failed",
                extra={"agent_id": self._agent_id, "error": str(exc)},
            )

    async def _ingest(self, event: BehavioralEvent) -> None:
        payload = event.payload or {}
        score = payload.get("score")
        if score is None:
            return
        # Prefer an explicit skill_id; fall back to the bandit's
        # candidate_idx so bench/test emissions without skill metadata
        # still aggregate onto *some* arm rather than being silently
        # dropped. The aggregation key is always a string so the
        # audit log stays uniform.
        skill_id = payload.get("skill_id")
        if skill_id is None:
            idx = payload.get("candidate_idx")
            if idx is None:
                return
            skill_id = f"candidate_idx:{int(idx)}"
        version = int(payload.get("version", 0))
        key = (str(skill_id), version)

        async with self._lock:
            arm = self._arms.get(key)
            if arm is None:
                arm = _ArmAggregate(skill_id=str(skill_id), version=version)
                self._arms[key] = arm
            arm.plays += 1
            arm.total_reward += float(score)

    # ── decision ─────────────────────────────────────────────────────

    async def evaluate(
        self,
        *,
        head_version: int | None = None,
        head_mean: float | None = None,
    ) -> EvolutionReport:
        """Call the controller with the current aggregate + log the decision.

        On PROMOTE, publishes a :data:`EventType.SKILL_CANDIDATE_PROPOSED`
        event carrying the evidence verbatim. The main agent's turn loop
        (or whatever is watching) decides whether to actually promote;
        the observer never writes to the :class:`SkillRegistry` — that
        would violate anti-req #12's structural enforcement (the
        evidence list must pass through registry.promote, not around it).
        """
        async with self._lock:
            evaluations = self.snapshot()
        report = self._controller.consider_promotion(
            evaluations, head_version=head_version, head_mean=head_mean,
        )
        self._append_audit(report, evaluations, head_version=head_version)
        if report.decision == EvolutionDecision.PROMOTE:
            await self._emit_proposal(report)
        return report

    async def _emit_proposal(self, report: EvolutionReport) -> None:
        """Publish a SKILL_CANDIDATE_PROPOSED event for a PROMOTE report.

        Session id is synthetic — the observer runs outside any WS
        turn. The ``agent_id`` on the event is the observer's own id,
        which the UI uses to attribute the proposal back to the
        workspace that emitted it.
        """
        event = make_event(
            session_id=f"evolution:{self._agent_id}",
            agent_id=self._agent_id,
            type=EventType.SKILL_CANDIDATE_PROPOSED,
            payload={
                "winner_candidate_id": report.winner_candidate_id,
                "winner_version": report.winner_version,
                "evidence": list(report.evidence),
                "reason": report.reason,
            },
        )
        await self._bus.publish(event)

    # ── audit ─────────────────────────────────────────────────────────

    def _append_audit(
        self,
        report: EvolutionReport,
        evaluations: list[CandidateEvaluation],
        *,
        head_version: int | None,
    ) -> None:
        """Append one JSONL line. Creates the parent dir on first call.

        Swallows OSError: the observer must keep running even when the
        audit dir is temporarily unwritable (disk full, permissions
        mid-flight). A warning-log line replaces the missing row — the
        in-memory aggregate is still authoritative so the next
        successful write catches up.
        """
        record = {
            "ts": time.time(),
            "agent_id": self._agent_id,
            "decision": report.decision.value,
            "head_version": head_version,
            "winner_candidate_id": report.winner_candidate_id,
            "winner_version": report.winner_version,
            "evaluations": [
                {
                    "candidate_id": e.candidate_id,
                    "version": e.version,
                    "plays": e.plays,
                    "mean_score": e.mean_score,
                }
                for e in evaluations
            ],
            "evidence": list(report.evidence),
            "reason": report.reason,
        }
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False))
                fh.write("\n")
        except OSError as exc:
            log.warning(
                "evolution.audit_write_failed",
                extra={"agent_id": self._agent_id, "error": str(exc)},
            )

    # ── test helpers ─────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all aggregated stats. Tests only."""
        self._arms.clear()
