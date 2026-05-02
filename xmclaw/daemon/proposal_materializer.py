"""ProposalMaterializer — turn ``SKILL_CANDIDATE_PROPOSED`` events into
real, immediately-usable skills.

B-167. Pre-B-167 the loop was broken in the middle:

    SkillProposer drafts ProposedSkill (body, evidence, triggers)
        ↓ emits SKILL_CANDIDATE_PROPOSED with winner_version=0
    EvolutionOrchestrator (auto_apply path) tries `registry.promote(id, 0)`
        ↓ but v0 isn't *registered*, so promote raises UnknownVersion
    UI / CLI shows "approve" button
        ↓ approval forwards to /api/v2/skills/<id>/promote
    same failure — there's nothing to promote to

The missing step was: write the body to disk + register a v1 *first*.
This file does exactly that, on every ``decision="propose"`` event:

  1. Render YAML frontmatter (description / triggers / created_by /
     evidence / confidence / source_pattern) + body → write
     ``~/.xmclaw/skills_user/<skill_id>/SKILL.md``.

  2. Wrap the body in :class:`MarkdownProcedureSkill` (same class
     ``UserSkillsLoader`` uses for hand-installed SKILL.md).

  3. Register in :class:`SkillRegistry` with
     ``manifest.created_by="evolved"`` + the proposal's evidence
     embedded. ``set_head=True`` so the agent picks it up on the
     next turn — no manual approve step.

  4. Idempotent: if ``skill_id`` already exists in the registry we
     skip silently (the proposer can re-emit the same draft on the
     next dream tick; we don't want to double-register or clobber
     a user-installed v1).

Anti-req #12 stays honoured: ``register()`` doesn't require the
evidence-list-on-promote check, but the manifest carries the evidence
verbatim so the audit answer to "why was this skill activated" is one
``registry.ref(...).manifest.evidence`` lookup away.

Lifespan-managed: ``app.py`` constructs one instance per daemon, calls
``start()`` once after orchestrator + skill_dream are up, and ``stop()``
in the shutdown ordering (between realtime trigger stop and skill_dream
stop).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.core.bus.memory import Subscription
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry
from xmclaw.utils.paths import user_skills_dir

_log = logging.getLogger(__name__)


def _render_frontmatter(
    *,
    description: str,
    triggers: list[str] | tuple[str, ...],
    evidence: list[str] | tuple[str, ...],
    confidence: float,
    source_pattern: str,
) -> str:
    """YAML frontmatter for an evolution-produced SKILL.md.

    Kept tiny on purpose: ``description`` is the LLM-picker hook,
    ``triggers`` lets ``MarkdownProcedureSkill`` route by keyword (B-29
    style), the rest is audit metadata so a future reader knows where
    this skill came from. We never embed the journal text itself —
    just the session IDs — because journals can contain user PII.
    """
    def _yaml_list(items: list[str] | tuple[str, ...]) -> str:
        if not items:
            return "[]"
        return "[" + ", ".join(repr(str(x)) for x in items) + "]"

    desc_clean = (description or "").replace("---", "—").strip()
    pat_clean = (source_pattern or "").replace("---", "—").strip()
    lines = [
        "---",
        f"description: {desc_clean}" if desc_clean else "description: ",
        f"triggers: {_yaml_list(triggers)}",
        "created_by: evolved",
        f"evidence: {_yaml_list(evidence)}",
        f"confidence: {confidence:.3f}",
        f"source_pattern: {pat_clean}",
        f"materialized_at: {time.time():.0f}",
        "---",
        "",
    ]
    return "\n".join(lines)


class ProposalMaterializer:
    """Subscribe to ``SKILL_CANDIDATE_PROPOSED`` and materialize drafts.

    Parameters
    ----------
    registry
        The ``SkillRegistry`` the daemon already exposes via
        ``orchestrator.registry``.
    bus
        Shared event bus. We attach a predicate that filters on
        ``decision="propose"`` only — ``decision="promote"`` and
        ``"rollback"`` are the orchestrator's job, not ours.
    skills_root
        Where to write ``<skill_id>/SKILL.md``. Defaults to
        :func:`user_skills_dir` (``~/.xmclaw/skills_user/``) — the
        canonical first-priority root the user_loader scans on
        boot.
    enabled
        Off-switch for tests and users who want the manual-approve
        flow back. Default ON because the manual flow is currently
        broken (B-167 root cause).
    """

    def __init__(
        self,
        registry: SkillRegistry,
        bus: InProcessEventBus,
        *,
        skills_root: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._root = skills_root if skills_root is not None else user_skills_dir()
        self._enabled = bool(enabled)
        self._subscription: Subscription | None = None
        self._materialized_count: int = 0
        self._skipped_count: int = 0

    # ── observability ───────────────────────────────────────────────

    @property
    def materialized_count(self) -> int:
        return self._materialized_count

    @property
    def skipped_count(self) -> int:
        return self._skipped_count

    @property
    def is_active(self) -> bool:
        return self._subscription is not None

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe. Idempotent. No-op when disabled."""
        if not self._enabled:
            return
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe(
            self._predicate, self._on_event,
        )
        _log.info(
            "proposal_materializer.start root=%s", self._root,
        )

    async def stop(self) -> None:
        """Unsubscribe. Idempotent."""
        sub = self._subscription
        self._subscription = None
        if sub is not None:
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001 — shutdown path, log + continue
                pass

    # ── subscription handler ────────────────────────────────────────

    def _predicate(self, event: BehavioralEvent) -> bool:
        if event.type is not EventType.SKILL_CANDIDATE_PROPOSED:
            return False
        decision = str((event.payload or {}).get("decision", "")).lower()
        # Orchestrator handles promote/rollback; we only handle the
        # net-new "propose" case.
        return decision == "propose"

    async def _on_event(self, event: BehavioralEvent) -> None:
        payload = event.payload or {}
        skill_id = payload.get("winner_candidate_id")
        if not isinstance(skill_id, str) or not skill_id:
            _log.warning(
                "proposal_materializer.malformed_payload event_id=%s",
                event.id,
            )
            return

        # Idempotent: a re-emitted same draft must NOT explode. The
        # skill might have been materialized by a prior tick of the
        # cycle; or the user might already have a same-id skill via
        # ``UserSkillsLoader``. Either way, we leave the existing
        # registry entry intact.
        if skill_id in self._registry.list_skill_ids():
            self._skipped_count += 1
            _log.info(
                "proposal_materializer.skip_already_registered skill_id=%s",
                skill_id,
            )
            return

        draft = payload.get("draft") or {}
        body = str(draft.get("body") or "").strip()
        if not body:
            _log.warning(
                "proposal_materializer.empty_body skill_id=%s — skipping",
                skill_id,
            )
            return

        title = str(draft.get("title") or skill_id)
        description = str(draft.get("description") or title)
        triggers = list(draft.get("triggers") or ())
        confidence = float(draft.get("confidence") or 0.0)
        evidence = list(payload.get("evidence") or ())
        source_pattern = str(payload.get("reason") or "")

        # Anti-req #12 spirit: even though register() doesn't enforce
        # it, refuse to materialize an evolved skill with no evidence.
        # The proposer should never emit this, but if it does we'd
        # rather fail loud than write a phantom skill.
        if not evidence:
            _log.warning(
                "proposal_materializer.no_evidence skill_id=%s — skipping",
                skill_id,
            )
            return

        # Compose the on-disk SKILL.md. Frontmatter first so the
        # user_loader on next boot reads the same metadata.
        frontmatter = _render_frontmatter(
            description=description,
            triggers=triggers,
            evidence=evidence,
            confidence=confidence,
            source_pattern=source_pattern,
        )
        full_text = frontmatter + body.rstrip() + "\n"

        skill_dir = self._root / skill_id
        skill_path = skill_dir / "SKILL.md"
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(full_text, encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "proposal_materializer.write_failed "
                "skill_id=%s path=%s err=%s",
                skill_id, skill_path, exc,
            )
            return

        # Register in the live registry so the agent sees the new tool
        # on its very next turn (SkillToolProvider re-reads the registry
        # snapshot per turn).
        skill = MarkdownProcedureSkill(
            id=skill_id, body=body, version=1,
        )
        manifest = SkillManifest(
            id=skill_id,
            version=1,
            title=title,
            description=description,
            created_by="evolved",
            evidence=tuple(evidence),
            triggers=tuple(triggers),
        )
        try:
            self._registry.register(skill, manifest, set_head=True)
        except ValueError as exc:
            # Race: same skill_id arrived twice in the same tick. The
            # first register won; we skip the second.
            _log.info(
                "proposal_materializer.register_race skill_id=%s err=%s",
                skill_id, exc,
            )
            self._skipped_count += 1
            return

        self._materialized_count += 1
        _log.info(
            "proposal_materializer.materialized "
            "skill_id=%s confidence=%.2f evidence_n=%d path=%s",
            skill_id, confidence, len(evidence), skill_path,
        )
