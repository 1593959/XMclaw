"""Autonomous skill trigger engine — keyword, event, and cron-based.

Three trigger types, all registered per-skill via manifest:

  keyword    — fires when user message contains trigger phrases
  event      — fires on matching daemon bus events (file_changed, memory_updated, …)
  cron       — fires on a recurring schedule (``0 */6 * * *``)

Triggers are evaluated once per agent turn / daemon tick. When a trigger
fires, the skill is force-injected into the next turn's tool list regardless
of prefilter score — ensuring autonomous execution even for skills the LLM
wouldn't normally pick.

Architecture: TriggerEngine is a singleton registered on the daemon's
``app.state``, polled by AgentLoop before each turn.

Reference: Claude Code skill lifecycle (description-driven),
           Mem0 autonomous memory ops (event-driven).
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Any

from croniter import croniter

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class TriggerEngine:
    """Evaluates skill triggers and force-injects matching skills."""

    def __init__(self, bus: Any = None) -> None:
        self._keyword_index: dict[str, list[str]] = defaultdict(list)
        self._event_index: dict[str, list[str]] = defaultdict(list)
        self._cron_entries: list[_CronEntry] = []
        self._skill_triggers: dict[str, dict[str, Any]] = {}
        self._cooldowns: dict[str, float] = {}
        self._bus = bus

    # ── registration ──────────────────────────────────────────────

    def register(self, skill_id: str, manifest: dict[str, Any]) -> None:
        triggers = manifest.get("triggers", {})
        if not triggers:
            return
        self._skill_triggers[skill_id] = triggers

        for phrase in (triggers.get("keywords") or []):
            key = phrase.strip().lower()
            if key:
                self._keyword_index[key].append(skill_id)

        for ev in (triggers.get("events") or []):
            evt = ev.strip().lower()
            if evt:
                self._event_index[evt].append(skill_id)

        cron_expr = triggers.get("cron")
        if cron_expr:
            self._cron_entries.append(_CronEntry(
                skill_id=skill_id,
                expr=str(cron_expr),
                last_fired=0.0,
            ))

        _log.info("trigger_engine.registered skill=%s types=%s", skill_id,
                   [k for k in ("keywords", "events", "cron") if triggers.get(k)])

    def unregister(self, skill_id: str) -> None:
        self._skill_triggers.pop(skill_id, None)
        for k, v in list(self._keyword_index.items()):
            self._keyword_index[k] = [s for s in v if s != skill_id]
            if not self._keyword_index[k]:
                del self._keyword_index[k]
        for k, v in list(self._event_index.items()):
            self._event_index[k] = [s for s in v if s != skill_id]
            if not self._event_index[k]:
                del self._event_index[k]
        self._cron_entries = [e for e in self._cron_entries if e.skill_id != skill_id]

    # ── evaluation ────────────────────────────────────────────────

    def evaluate_keywords(self, user_message: str) -> list[str]:
        """Return skill_ids whose keyword triggers match the user message."""
        lowered = user_message.lower()
        matched: list[str] = []
        for phrase, skill_ids in self._keyword_index.items():
            if phrase in lowered:
                for sid in skill_ids:
                    if self._check_cooldown(sid):
                        matched.append(sid)
                        _log.info("trigger_engine.keyword_fired skill=%s phrase=%r", sid, phrase)
        return matched

    def evaluate_events(self, event_type: str) -> list[str]:
        """Return skill_ids whose event triggers match the given event type."""
        evt = event_type.lower()
        skill_ids = self._event_index.get(evt, [])
        return [sid for sid in skill_ids if self._check_cooldown(sid)]

    def evaluate_cron(self) -> list[str]:
        """Return skill_ids whose cron triggers are due."""
        now = time.time()
        matched: list[str] = []
        for entry in self._cron_entries:
            try:
                c = croniter(entry.expr, entry.last_fired)
                next_fire = c.get_next()
                if now >= next_fire:
                    if self._check_cooldown(entry.skill_id):
                        matched.append(entry.skill_id)
                        entry.last_fired = now
                        _log.info("trigger_engine.cron_fired skill=%s expr=%s", entry.skill_id, entry.expr)
            except Exception:
                pass
        return matched

    def evaluate_all(self, *, user_message: str = "", event_type: str = "") -> list[str]:
        """Run all trigger evaluations. Returns deduped list of skill_ids to force-inject."""
        results: set[str] = set()
        if user_message:
            results.update(self.evaluate_keywords(user_message))
        if event_type:
            results.update(self.evaluate_events(event_type))
        results.update(self.evaluate_cron())
        return list(results)

    # ── cooldown ──────────────────────────────────────────────────

    def _check_cooldown(self, skill_id: str) -> bool:
        triggers = self._skill_triggers.get(skill_id, {})
        cooldown_s = int(triggers.get("cooldown_seconds", 30))
        last = self._cooldowns.get(skill_id, 0.0)
        if time.time() - last < cooldown_s:
            return False
        self._cooldowns[skill_id] = time.time()
        return True


class _CronEntry:
    __slots__ = ("skill_id", "expr", "last_fired")

    def __init__(self, skill_id: str, expr: str, last_fired: float) -> None:
        self.skill_id = skill_id
        self.expr = expr
        self.last_fired = last_fired
