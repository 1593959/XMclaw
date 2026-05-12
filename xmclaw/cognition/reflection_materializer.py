"""ReflectionMaterializer — close the loop on agent reflection.

Pre-2026-05-12 state
====================

Inner monologue / metacognition events were observability-only. The
agent thought "下次遇到 X 我应该 Y", that thought was published as
``INNER_MONOLOGUE`` → stored in events.db → displayed in the Mind
page. **Nothing else consumed it.** The agent's next turn rebuilt
its system prompt from persona files which had no record of the
plan it just generated — so the next time X happened, the agent
forgot it ever planned to handle X differently. Same for
``METACOGNITION_PROPOSAL`` from the R3 Reformer: emitted to the bus,
displayed in SuggestionInbox, awaited a human click.

What this module does
=====================

Subscribes to the two reflection event streams and materialises them
into the persona files that DO drive the next turn's system prompt:

  INNER_MONOLOGUE kind=plan          → AGENTS.md  ## Auto-extracted reflections
  INNER_MONOLOGUE kind=concern       → MEMORY.md  ## Failure Modes
  METACOGNITION_PROPOSAL kind=preference_update  → USER.md   ## Auto-extracted preferences
  METACOGNITION_PROPOSAL kind=curriculum_edit    → AGENTS.md ## Auto-extracted curriculum
  METACOGNITION_PROPOSAL kind=skill_propose      → (no-op; ProposalMaterializer
                                                    already handles skill writes)

Each write goes through the same primitives ExtractLessonsHook uses
— ``_append_under_section`` + ``enforce_char_cap`` + per-file lock
— so the audit trail and char-cap eviction look identical, and the
persona assembler's existing render pipeline picks them up
automatically on the next system-prompt build.

Why this is safe to auto-apply
==============================

Every write is:

* **Reversible** — bullets are date-stamped + sectioned, easy to
  diff or `git restore` the persona file.
* **Capped** — `PERSONA_CHAR_CAPS` evicts the oldest bullets when a
  file outgrows its budget. Plans can't accrete forever.
* **Confidence-gated** — METACOGNITION_PROPOSAL.confidence is
  already capped at 0.6 (Iron Rule #2) by MetaCognitionPass.
  We require ≥ 0.3 for preferences, ≥ 0.4 for curriculum.
  INNER_MONOLOGUE plan thoughts default to 0.5.
* **Rate-limited** — `_per_kind_minute_quota` prevents a chatty
  LLM from spamming bullets faster than they can be reviewed.

Why we DON'T auto-apply skill_propose
=====================================

Skill materialization is already handled by
``ProposalMaterializer`` (writes SKILL.md from
``SKILL_CANDIDATE_PROPOSED`` events). Adding a second path from the
Reformer's skill_propose would dual-write the same skill or
generate near-duplicates. Keep one materializer per artifact type.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# ── Routing table ──────────────────────────────────────────────────────
#
# Each entry: ``(target_file, section_header, char_cap_key, min_confidence)``.
# char_cap_key is the same key used by PERSONA_CHAR_CAPS so eviction
# stays consistent across all auto-extract writers.

_INNER_MONOLOGUE_ROUTES: dict[str, tuple[str, str, float]] = {
    # kind → (target_file, section_header, min_confidence)
    "plan":    ("AGENTS.md", "## Auto-extracted reflections", 0.0),
    "concern": ("MEMORY.md", "## Failure Modes", 0.0),
    # observation / wonder / hypothesis: descriptive only — don't
    # materialize unless the LLM upgrades them to plan/concern.
}

_METACOG_ROUTES: dict[str, tuple[str, str, float]] = {
    # kind → (target_file, section_header, min_confidence)
    "preference_update": ("USER.md",   "## Auto-extracted preferences", 0.3),
    "curriculum_edit":   ("AGENTS.md", "## Auto-extracted curriculum",  0.4),
    # skill_propose handled by ProposalMaterializer — see module docstring.
}


# ── Rate limit ─────────────────────────────────────────────────────────
#
# A buggy LLM (or a bad prompt) can flood the bus with reflections.
# Cap each kind to N writes per rolling window. Bullets that lose the
# cap are dropped silently — they'll be re-proposed if the pattern
# remains real, since the next reflection cycle re-scans the same
# event window.

_QUOTA_WINDOW_S: float = 3600.0  # 1 hour
_QUOTA_PER_KIND: dict[str, int] = {
    "plan": 4,
    "concern": 3,
    "preference_update": 4,
    "curriculum_edit": 3,
}


class ReflectionMaterializer:
    """Bus subscriber that writes reflection events into persona files.

    Constructor params:

    * ``bus`` — the SqliteEventBus / InProcessEventBus the daemon
      runs on. Subscribed for INNER_MONOLOGUE + METACOGNITION_PROPOSAL.
    * ``persona_dir_provider`` — zero-arg callable returning the
      currently-active persona dir as Path. Resolved on every event
      so persona switches mid-session are honoured.
    * ``cfg`` — daemon config dict; only ``cognition.reflection_materialize.*``
      is read. Default-on; flip ``enabled: false`` to disable.
    """

    def __init__(
        self,
        *,
        bus: Any,
        persona_dir_provider: Any,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self._bus = bus
        self._persona_dir_provider = persona_dir_provider
        self._cfg = ((cfg or {}).get("cognition") or {}).get(
            "reflection_materialize"
        ) or {}
        self._enabled = bool(self._cfg.get("enabled", True))
        # Bus-subscription handle for stop().
        self._sub_handle: Any = None
        # Per-kind sliding-window quota: kind → [ts, ts, …].
        self._recent_writes: dict[str, list[float]] = {}

    async def start(self) -> None:
        """Subscribe to the bus. Idempotent."""
        if not self._enabled:
            logger.info("reflection_materializer.disabled")
            return
        if self._sub_handle is not None:
            return
        try:
            from xmclaw.core.bus import EventType
            wanted = {
                EventType.INNER_MONOLOGUE,
                EventType.METACOGNITION_PROPOSAL,
            }
        except Exception:  # noqa: BLE001
            logger.warning(
                "reflection_materializer.event_types_unavailable",
            )
            return

        # Bus contract (xmclaw.core.bus.memory.Subscription):
        #   subscribe(predicate, handler) — predicate is SYNC bool,
        #   handler is async; returns a Subscription whose .cancel()
        #   we call in stop().
        def _filter(event: Any) -> bool:
            try:
                return event.type in wanted
            except Exception:  # noqa: BLE001
                return False

        try:
            self._sub_handle = self._bus.subscribe(
                _filter, self._on_event,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection_materializer.subscribe_failed err=%s", exc,
            )
            return
        logger.info("reflection_materializer.start")

    async def stop(self) -> None:
        """Cancel the subscription. Idempotent."""
        h = self._sub_handle
        self._sub_handle = None
        if h is None:
            return
        try:
            # Subscription.cancel() — see xmclaw/core/bus/memory.py.
            # Both InProcessEventBus and SqliteEventBus expose the same
            # cancel-able handle shape.
            if hasattr(h, "cancel"):
                h.cancel()
        except Exception:  # noqa: BLE001
            pass

    # ── Event handler ──────────────────────────────────────────────

    async def _on_event(self, event: Any) -> None:
        """Bus callback. NEVER raises — best-effort materialization."""
        try:
            etype = str(getattr(event.type, "value", event.type))
            payload = event.payload or {}
            if etype == "inner_monologue":
                await self._handle_inner_monologue(payload, event)
            elif etype == "metacognition_proposal":
                await self._handle_metacog_proposal(payload, event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection_materializer.handler_failed err=%s", exc,
            )

    async def _handle_inner_monologue(
        self, payload: dict[str, Any], event: Any,
    ) -> None:
        kind = str(payload.get("kind", "")).strip().lower()
        text = str(payload.get("text", "")).strip()
        if not text or kind not in _INNER_MONOLOGUE_ROUTES:
            return
        target_file, section, min_conf = _INNER_MONOLOGUE_ROUTES[kind]
        # InnerThought has no explicit confidence — treat as 0.5 default
        # so the floor is whatever the route requires (0.0 today, but
        # routes can tighten without changing call sites).
        if 0.5 < min_conf:
            return
        if not self._consume_quota(kind):
            return
        await self._append_bullet(
            target_file=target_file,
            section=section,
            bullet_text=self._format_inner_monologue_bullet(payload),
        )

    async def _handle_metacog_proposal(
        self, payload: dict[str, Any], event: Any,
    ) -> None:
        kind = str(payload.get("kind", "")).strip().lower()
        if kind not in _METACOG_ROUTES:
            return  # skill_propose / no_op handled elsewhere
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        target_file, section, min_conf = _METACOG_ROUTES[kind]
        if confidence < min_conf:
            logger.info(
                "reflection_materializer.gated kind=%s "
                "confidence=%.2f<%.2f", kind, confidence, min_conf,
            )
            return
        if not self._consume_quota(kind):
            return
        await self._append_bullet(
            target_file=target_file,
            section=section,
            bullet_text=self._format_metacog_bullet(payload),
        )

    # ── Quota ──────────────────────────────────────────────────────

    def _consume_quota(self, kind: str) -> bool:
        """Sliding-window rate-limit. Returns True iff a slot was free."""
        cap = _QUOTA_PER_KIND.get(kind)
        if cap is None or cap <= 0:
            return True
        now = time.time()
        recent = self._recent_writes.setdefault(kind, [])
        # Drop entries outside the window.
        recent[:] = [t for t in recent if (now - t) < _QUOTA_WINDOW_S]
        if len(recent) >= cap:
            logger.info(
                "reflection_materializer.quota_exhausted kind=%s "
                "cap=%d window_s=%.0f", kind, cap, _QUOTA_WINDOW_S,
            )
            return False
        recent.append(now)
        return True

    # ── Formatting ─────────────────────────────────────────────────

    @staticmethod
    def _format_inner_monologue_bullet(payload: dict[str, Any]) -> str:
        text = str(payload.get("text", "")).strip()
        trigger = str(payload.get("trigger", "")).strip()
        # Strip leading dates/timestamps the LLM occasionally prefixes
        # — _append_under_section will prepend a fresh date below.
        text = text.replace("\n", " ").strip()
        if trigger and trigger != "recent_events":
            return f"{text}  *(trigger: {trigger[:80]})*"
        return text

    @staticmethod
    def _format_metacog_bullet(payload: dict[str, Any]) -> str:
        why = str(payload.get("why", "")).strip()
        # ``why`` already includes the pattern summary; fall back to
        # ``pattern_summary`` if the upstream contract changes.
        if not why:
            why = str(payload.get("pattern_summary", "")).strip()
        # ``payload`` is the structured proposal payload — for the kinds
        # we materialize, it carries the user-facing rule text.
        inner = payload.get("payload") or {}
        if isinstance(inner, dict):
            rule = (
                inner.get("rule")
                or inner.get("text")
                or inner.get("preference")
                or inner.get("guidance")
                or ""
            )
            rule = str(rule).strip()
        else:
            rule = ""
        if rule:
            return f"{rule}  *({why[:120]})*" if why else rule
        return why or "(unspecified metacognition proposal)"

    # ── Write path ─────────────────────────────────────────────────

    async def _append_bullet(
        self, *, target_file: str, section: str, bullet_text: str,
    ) -> None:
        """Append a date-stamped bullet under ``section`` in
        ``<persona>/target_file``. Atomic write + cap eviction.

        Mirrors ExtractLessonsHook's write path so the audit trail
        looks the same: identical section format, identical date
        prefix, identical PERSONA_CHAR_CAPS budget.
        """
        try:
            from xmclaw.providers.tool.builtin import (
                PERSONA_CHAR_CAPS,
                _append_under_section,
                enforce_char_cap,
            )
            from xmclaw.utils.fs_locks import atomic_write_text, get_lock
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection_materializer.imports_unavailable err=%s", exc,
            )
            return

        try:
            persona_dir = self._persona_dir_provider()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflection_materializer.persona_dir_failed err=%s", exc,
            )
            return
        if persona_dir is None:
            return
        pdir = Path(str(persona_dir))
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "reflection_materializer.mkdir_failed err=%s", exc,
            )
            return
        mfile = pdir / target_file
        date = time.strftime("%Y-%m-%d")
        bullet = f"- {date}: {bullet_text}"

        try:
            async with get_lock(mfile):
                existing = (
                    mfile.read_text(encoding="utf-8")
                    if mfile.is_file() else ""
                )
                new_text = _append_under_section(
                    existing,
                    section_header=section,
                    bullet=bullet,
                    placeholder_title=f"{target_file} — auto-extracted",
                )
                cap = PERSONA_CHAR_CAPS.get(target_file)
                if cap is not None and len(new_text) > cap:
                    new_text = enforce_char_cap(new_text, cap)
                if new_text != existing:
                    atomic_write_text(mfile, new_text)
                    logger.info(
                        "reflection_materializer.wrote file=%s section=%r "
                        "bullet_len=%d",
                        target_file, section, len(bullet_text),
                    )
                    # Invalidate the system-prompt cache so the next
                    # turn's persona assembler re-reads the freshly
                    # appended bullet. Without this, the loop is
                    # closed on disk but the agent only picks it up
                    # after a manual restart or another bump.
                    try:
                        from xmclaw.daemon.prompt_builder import (
                            bump_prompt_freeze_generation,
                        )
                        bump_prompt_freeze_generation()
                    except Exception:  # noqa: BLE001
                        pass
        except OSError as exc:
            logger.warning(
                "reflection_materializer.write_failed file=%s err=%s",
                target_file, exc,
            )


__all__ = ["ReflectionMaterializer"]
