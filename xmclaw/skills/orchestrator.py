"""EvolutionOrchestrator — bridge SkillRegistry mutations to the event bus.

Epic #4 Phase B. The registry enforces anti-req #12 at the source
(``promote()`` without evidence raises) but there is no visibility
layer: a caller flips HEAD, a line lands in ``~/.xmclaw/skills/<id>.jsonl``,
and nothing else notices. ``session report`` can't reconstruct the
evolution, a future REPL can't flash ``[evolved] ...``, Grafana has
nothing to plot.

This class wraps :meth:`SkillRegistry.promote` / :meth:`rollback` with
matching :data:`EventType.SKILL_PROMOTED` / :data:`SKILL_ROLLED_BACK`
bus events. One caller, one event, no drift. The registry mutation
stays authoritative — the bus event is strictly a broadcast of what
just happened, so a bus-level failure never leaves HEAD inconsistent
with the audit log.

The orchestrator also optionally subscribes to
:data:`EventType.SKILL_CANDIDATE_PROPOSED` (emitted by
:class:`~xmclaw.daemon.evolution_agent.EvolutionAgent` after the
controller clears all gates) and auto-applies the proposal by calling
:meth:`promote`. This is **opt-in** (``auto_apply=False`` by default)
because a freshly installed daemon must not silently start mutating
HEAD on its own — the user's first contact with evolution is always
an explicit opt-in. When ``auto_apply=True``, the orchestrator still
hands the proposal's evidence verbatim to the registry, so anti-req
#12 keeps teeth regardless of how the promotion was triggered.

Placement note: lives in ``xmclaw/skills/`` rather than
``xmclaw/daemon/`` because its primary dependency (``SkillRegistry``)
is a skills-layer type. Skills may import from ``core/`` for the bus,
which is exactly the edge this module walks.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType, make_event
from xmclaw.core.bus.memory import Subscription
from xmclaw.skills.registry import SkillRegistry, UnknownSkillError
from xmclaw.skills.versioning import PromotionRecord

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class EvolutionOrchestrator:
    """Bus-aware wrapper over :class:`SkillRegistry`.

    Call :meth:`promote` / :meth:`rollback` instead of touching the
    registry directly when the mutation should be observable on the
    event bus. Call :meth:`start` to subscribe to proposal events when
    ``auto_apply=True``; stop cancels the subscription.

    Parameters
    ----------
    registry : SkillRegistry
        The registry whose HEAD is being mutated.
    bus : InProcessEventBus
        Event bus for the matching ``SKILL_PROMOTED`` / ``SKILL_ROLLED_BACK``
        broadcasts.
    agent_id : str, default "orchestrator"
        ``agent_id`` stamped on emitted events when the call site
        doesn't override it. The REPL / UI uses this to attribute the
        mutation back to the pipeline that made it.
    auto_apply : bool, default False
        Subscribe to ``SKILL_CANDIDATE_PROPOSED`` and auto-promote on
        every proposal. Off by default — see module docstring.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        bus: InProcessEventBus,
        *,
        agent_id: str = "orchestrator",
        auto_apply: bool = False,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._agent_id = agent_id
        self._auto_apply = auto_apply
        self._subscription: Subscription | None = None

    # ── public lifecycle ─────────────────────────────────────────────

    @property
    def registry(self) -> SkillRegistry:
        """Expose the wrapped registry for read-only inspection.

        Used by the ``/api/v2/skills`` HTTP surface to enumerate
        registered skill versions + HEAD without leaking the
        orchestrator's mutation API to HTTP callers.
        """
        return self._registry

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def auto_apply(self) -> bool:
        return self._auto_apply

    def is_running(self) -> bool:
        return self._subscription is not None

    async def start(self) -> None:
        """Subscribe to proposal events. No-op when ``auto_apply=False``.

        Idempotent — a second ``start`` is a no-op rather than a
        double subscription (would otherwise double-apply each
        proposal).
        """
        if self._subscription is not None:
            return
        if not self._auto_apply:
            return
        self._subscription = self._bus.subscribe(
            lambda e: e.type == EventType.SKILL_CANDIDATE_PROPOSED,
            self._on_proposal,
        )
        log.info(
            "orchestrator.start",
            extra={"agent_id": self._agent_id, "auto_apply": True},
        )

    async def stop(self) -> None:
        """Cancel the subscription. Idempotent."""
        if self._subscription is None:
            return
        self._subscription.cancel()
        self._subscription = None
        log.info("orchestrator.stop", extra={"agent_id": self._agent_id})

    # ── public mutation ──────────────────────────────────────────────

    async def promote(
        self,
        skill_id: str,
        to_version: int,
        *,
        evidence: list[str],
        session_id: str = "_system",
        agent_id: str | None = None,
        source: str = "manual",
    ) -> PromotionRecord:
        """Move HEAD and broadcast a ``SKILL_PROMOTED`` event.

        Evidence is passed verbatim to ``registry.promote`` — anti-req
        #12 is still enforced at the registry door. If the registry
        refuses (empty evidence, unknown version), the exception
        propagates and NO event is emitted, so subscribers never see a
        phantom promotion.

        B-121: ``source`` is forwarded to the registry so the audit
        log records who-decided. Defaults to ``"manual"`` because the
        public method is what manual callers (REPL, scripts, future
        admin CLIs) reach for; ``_on_proposal`` overrides to
        ``"controller"`` for the auto-evolution path.
        """
        record = self._registry.promote(
            skill_id, to_version, evidence=evidence, source=source,
        )
        await self._emit(
            record,
            event_type=EventType.SKILL_PROMOTED,
            session_id=session_id,
            agent_id=agent_id or self._agent_id,
        )
        return record

    async def rollback(
        self,
        skill_id: str,
        to_version: int,
        *,
        reason: str,
        session_id: str = "_system",
        agent_id: str | None = None,
        source: str = "manual",
    ) -> PromotionRecord:
        """Move HEAD back and broadcast a ``SKILL_ROLLED_BACK`` event."""
        record = self._registry.rollback(
            skill_id, to_version, reason=reason, source=source,
        )
        await self._emit(
            record,
            event_type=EventType.SKILL_ROLLED_BACK,
            session_id=session_id,
            agent_id=agent_id or self._agent_id,
        )
        return record

    # ── internal ─────────────────────────────────────────────────────

    async def _on_proposal(self, event: BehavioralEvent) -> None:
        """Handle a ``SKILL_CANDIDATE_PROPOSED`` event.

        Wrapped so a single bad proposal (missing field, unknown skill
        id, registry race) logs + skips rather than killing the
        subscription task.

        B-119: payload now carries ``decision: "promote"|"rollback"``.
        Promote uses ``evidence`` (anti-req #12); rollback uses
        ``reason`` (registry.rollback's mandatory field). Default is
        promote when ``decision`` is missing — back-compat for older
        EvolutionAgent payloads that didn't emit the field.
        """
        payload = event.payload or {}
        decision = str(payload.get("decision", "promote")).lower()
        skill_id = payload.get("winner_candidate_id")
        to_version = payload.get("winner_version")
        if not isinstance(skill_id, str) or to_version is None:
            log.warning(
                "orchestrator.proposal_malformed",
                extra={"event_id": event.id, "payload": payload},
            )
            return
        try:
            if decision == "rollback":
                # Compose a reason from evidence + reason if both present.
                reason_parts: list[str] = []
                if payload.get("reason"):
                    reason_parts.append(str(payload["reason"]))
                evidence_list = payload.get("evidence") or []
                if isinstance(evidence_list, list) and evidence_list:
                    reason_parts.append("evidence: " + "; ".join(str(e) for e in evidence_list))
                reason = " — ".join(reason_parts) or "auto-rollback (controller)"
                await self.rollback(
                    skill_id,
                    int(to_version),
                    reason=reason,
                    session_id=event.session_id,
                    agent_id=event.agent_id,
                    source="controller",
                )
                return
            evidence = list(payload.get("evidence", []))
            if not evidence:
                log.warning(
                    "orchestrator.promote_no_evidence",
                    extra={"event_id": event.id, "payload": payload},
                )
                return
            await self.promote(
                skill_id,
                int(to_version),
                evidence=evidence,
                session_id=event.session_id,
                agent_id=event.agent_id,
                source="controller",
            )
        except (UnknownSkillError, ValueError) as exc:
            # Registry rejected — propagate as a log line, not a crash.
            # A real UI would pick this up via a future ANTI_REQ_VIOLATION
            # feed; for now the observer's audit log already has the
            # proposal and the registry history is untouched.
            log.warning(
                "orchestrator.promote_refused",
                extra={
                    "agent_id": self._agent_id,
                    "skill_id": skill_id,
                    "to_version": to_version,
                    "error": str(exc),
                },
            )

    async def _emit(
        self,
        record: PromotionRecord,
        *,
        event_type: EventType,
        session_id: str,
        agent_id: str,
    ) -> None:
        payload: dict[str, object] = {
            "skill_id": record.skill_id,
            "from_version": record.from_version,
            "to_version": record.to_version,
            "ts": record.ts,
            "evidence": list(record.evidence),
            "source": record.source,  # B-121
        }
        if record.reason is not None:
            payload["reason"] = record.reason
        event = make_event(
            session_id=session_id,
            agent_id=agent_id,
            type=event_type,
            payload=payload,
        )
        await self._bus.publish(event)
