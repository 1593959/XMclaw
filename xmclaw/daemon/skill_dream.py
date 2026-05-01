"""SkillDreamCycle — periodic SkillProposer driver. Epic #24 Phase 3.2.

Runs :class:`SkillProposer` on a configurable interval (default 30
minutes) and turns the ``ProposedSkill`` results into
``SKILL_CANDIDATE_PROPOSED`` events on the bus + a JSONL audit row in
``~/.xmclaw/v2/evolution/<agent_id>/proposals.jsonl``.

Distinct from the **memory dream** (``xmclaw.daemon.dream_compactor``),
which compacts MEMORY.md on a daily cron. This is the **skill
dream** — looking back at recent journal history and asking "what
recurring pattern deserves to be a skill?". Both run independently,
neither depends on the other.

Default behaviour
-----------------

The proposer is constructed with the default ``noop_extractor`` until
the daemon factory swaps in an LLM-backed one (Phase 3.3+). With the
no-op the cycle still runs (cheap pattern detection over the journal),
just emits zero proposals — enough to verify the wiring without
spending LLM tokens on every dev install.

Failure isolation
-----------------

A bad proposal payload, a slow LLM call, or a crash in the audit
write must NOT kill the dream task. We log + continue. The task
stops only on :meth:`stop` (lifespan-driven) or daemon process exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.evolution import SkillProposer
from xmclaw.utils.paths import evolution_dir

_log = logging.getLogger(__name__)


class SkillDreamCycle:
    """Periodic driver around :class:`SkillProposer`.

    Parameters
    ----------
    proposer : SkillProposer
        Pre-constructed proposer (with whatever extractor / journal
        reader / thresholds the caller picked).
    bus : InProcessEventBus
        Shared event bus. Each ``ProposedSkill`` becomes one
        ``SKILL_CANDIDATE_PROPOSED`` event with payload shape::

            {
              "decision": "propose",   # distinct from observer's
                                       # "promote" / "rollback"
              "winner_candidate_id": skill_id,
              "winner_version": 0,     # not yet in registry
              "evidence": [session_ids ...],
              "reason": source_pattern,
              "draft": {title, description, body, triggers,
                        confidence},
            }

        ``decision="propose"`` distinguishes this from the EvolutionAgent
        observer's promote/rollback proposals so downstream consumers
        can route appropriately (e.g. the future SkillProposer review
        UI vs the existing ``xmclaw evolve approve`` flow).
    agent_id : str, default "skill-dream"
        ``agent_id`` stamped on emitted events + path under
        ``evolution_dir()`` for audit log.
    interval_s : float, default 1800.0
        Seconds between cycles. 30 min default is conservative; with a
        real LLM extractor and 50 sessions of journal data the cycle
        cost dominates daily.
    enabled : bool, default True
        Off-switch for tests / users who don't want the periodic task.
        :meth:`start` is a no-op when False.
    audit_dir : Path | None
        Override audit root (tests pass tmp_path).
    """

    def __init__(
        self,
        proposer: SkillProposer,
        bus: InProcessEventBus,
        *,
        agent_id: str = "skill-dream",
        interval_s: float = 1800.0,
        enabled: bool = True,
        audit_dir: Path | None = None,
    ) -> None:
        self._proposer = proposer
        self._bus = bus
        self._agent_id = agent_id
        self._interval_s = max(1.0, float(interval_s))
        self._enabled = bool(enabled)
        base = audit_dir if audit_dir is not None else evolution_dir()
        self._audit_path = base / agent_id / "proposals.jsonl"
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ── public lifecycle ─────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def audit_path(self) -> Path:
        return self._audit_path

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the periodic task. Idempotent. No-op when disabled."""
        if not self._enabled:
            return
        if self.is_running():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="skill-dream-loop")
        _log.info(
            "skill_dream.start agent=%s interval_s=%.0f",
            self._agent_id, self._interval_s,
        )

    async def stop(self) -> None:
        """Stop the periodic task. Idempotent."""
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001 — propagation in shutdown
                # path is unhelpful; log was already emitted inside.
                pass

    async def _loop(self) -> None:
        # First run after `interval_s` rather than immediately —
        # avoids slamming the LLM on every daemon restart while there's
        # no fresh journal data yet.
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._interval_s,
                    )
                    # stop_event was set → exit cleanly.
                    return
                except asyncio.TimeoutError:
                    pass  # interval elapsed, run a cycle
                try:
                    await self.run_once()
                except Exception as exc:  # noqa: BLE001 — must not
                    # kill the loop. Logged at WARNING; next interval
                    # gets a fresh attempt.
                    _log.warning("skill_dream.cycle_failed err=%s", exc)
        except asyncio.CancelledError:
            return

    # ── one cycle ────────────────────────────────────────────────────

    async def run_once(self) -> int:
        """Run one detection→propose pass. Returns proposal count.

        Public so tests + the future REST endpoint
        (``POST /api/v2/skills/dream/run``) can trigger it directly
        without waiting for the next interval.
        """
        proposals = await self._proposer.propose()
        if not proposals:
            return 0
        for p in proposals:
            await self._emit_proposal(p)
            self._append_audit(p)
        return len(proposals)

    async def _emit_proposal(self, proposal) -> None:
        try:
            event = make_event(
                session_id=f"skill-dream:{self._agent_id}",
                agent_id=self._agent_id,
                type=EventType.SKILL_CANDIDATE_PROPOSED,
                payload={
                    "decision": "propose",
                    "winner_candidate_id": proposal.skill_id,
                    "winner_version": 0,
                    "evidence": list(proposal.evidence),
                    "reason": proposal.source_pattern,
                    "draft": {
                        "title": proposal.title,
                        "description": proposal.description,
                        "body": proposal.body,
                        "triggers": list(proposal.triggers),
                        "confidence": proposal.confidence,
                    },
                },
            )
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "skill_dream.publish_failed candidate=%s err=%s",
                proposal.skill_id, exc,
            )

    def _append_audit(self, proposal) -> None:
        record = {
            "ts": time.time(),
            "agent_id": self._agent_id,
            "skill_id": proposal.skill_id,
            "title": proposal.title,
            "confidence": proposal.confidence,
            "evidence": list(proposal.evidence),
            "source_pattern": proposal.source_pattern,
            "draft_body_chars": len(proposal.body or ""),
        }
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            _log.warning(
                "skill_dream.audit_write_failed agent=%s err=%s",
                self._agent_id, exc,
            )
