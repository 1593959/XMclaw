"""Cron-scheduled proactive trigger — Wave 11.

ProactiveAgent's built-in triggers fire when *conditions* match (idle,
calendar event imminent, project stale). This trigger fires when the
*clock* matches a schedule — "每天 8 点把日历摘要推过来"-style.

Why a separate trigger instead of reusing CronStore from
xmclaw/core/scheduler/cron.py: CronStore wakes the full AgentLoop with
a *prompt*; that's overkill for "remind me of X at time Y" where the
text is fixed. A trigger publishes PROACTIVE_PROPOSAL directly →
Web UI bubble + (Wave 9) feishu push, no LLM tokens burned for the
reminder itself.

Config shape (``cognition.proactive.cron_jobs``):

  [
    {
      "name": "morning_briefing",          # used for cooldown bookkeeping
      "schedule": "0 8 * * *",             # or "every 1h"
      "message": "☀️ 早安。今天日历：...",  # fixed text
      "urgency": "normal"                  # default normal
    },
    {
      "name": "lunch_break",
      "schedule": "0 12 * * MON-FRI",
      "message": "🍱 该吃午饭了"
    }
  ]

Schedule expressions:
  * ``"every Nu"``  — works without croniter (interval-based)
  * ``"0 8 * * *"`` — full cron, needs croniter (optional dep)

Bad schedule strings are skipped at registration time with a warning —
they don't crash the daemon. A trigger whose schedule fails to parse
just never fires.
"""
from __future__ import annotations

import time
from typing import Any

from xmclaw.cognition.proactive_agent import (
    ProactiveContext,
    ProactiveTrigger,
    TriggerProposal,
)
from xmclaw.core.scheduler.cron import parse_schedule
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


class CronTrigger(ProactiveTrigger):
    """Fires when the clock crosses ``schedule_expr`` next-fire time.

    Internally caches the next-fire epoch. On each tick:
      * If now < next_fire: should_fire returns False
      * If now >= next_fire: should_fire returns True; after propose
        emits the proposal, advances next_fire to the following slot

    Cooldown is set to 60s so we don't re-fire the same slot twice
    within one tick burst.
    """

    def __init__(
        self,
        *,
        name: str,
        schedule_expr: str,
        message: str,
        urgency: str = "normal",
    ) -> None:
        self.name = name
        self.cooldown_s = 60.0
        self._schedule_expr = schedule_expr
        self._message = message
        self._urgency = (
            urgency if urgency in ("low", "normal", "high") else "normal"
        )
        self._next_fire_ts: float | None = None
        # Initialize next fire so a brand-new trigger doesn't fire
        # immediately on the first tick.
        try:
            self._next_fire_ts = parse_schedule(
                schedule_expr, now=time.time(),
            )
        except ValueError as exc:
            logger.warning(
                "cron_trigger.bad_schedule name=%s expr=%r err=%s",
                name, schedule_expr, exc,
            )
            self._next_fire_ts = None

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        if self._next_fire_ts is None:
            return False
        return ctx.now >= self._next_fire_ts

    async def propose(
        self, ctx: ProactiveContext,
    ) -> TriggerProposal | None:
        if self._next_fire_ts is None:
            return None
        if ctx.now < self._next_fire_ts:
            return None
        # Advance to the next slot BEFORE building the proposal so a
        # crash after this point doesn't make us re-fire forever.
        try:
            self._next_fire_ts = parse_schedule(
                self._schedule_expr, now=ctx.now + 1.0,
            )
        except ValueError:
            # Schedule started parsing fine but suddenly fails — keep
            # firing at the same slot would spam, so disable.
            self._next_fire_ts = None
        return TriggerProposal(
            trigger_name=self.name,
            message=self._message,
            urgency=self._urgency,
            payload={
                "schedule": self._schedule_expr,
                "fired_at": ctx.now,
            },
        )


def build_cron_triggers_from_config(
    jobs_config: list[Any] | None,
) -> list[CronTrigger]:
    """Read ``cognition.proactive.cron_jobs`` and produce a list of
    ready-to-register CronTrigger instances. Malformed jobs are skipped
    with a warning — one typo doesn't disable the others."""
    if not jobs_config or not isinstance(jobs_config, list):
        return []
    out: list[CronTrigger] = []
    seen_names: set[str] = set()
    for idx, job in enumerate(jobs_config):
        if not isinstance(job, dict):
            logger.warning(
                "cron_trigger.skip idx=%d reason=not_a_dict", idx,
            )
            continue
        # Per-job enabled gate. Default True so existing minimal
        # configs (no "enabled" field) keep working.
        if job.get("enabled") is False:
            continue
        name = str(job.get("name") or "").strip()
        if not name:
            name = f"cron_{idx}"
        if name in seen_names:
            logger.warning(
                "cron_trigger.skip name=%s reason=duplicate", name,
            )
            continue
        schedule = str(job.get("schedule") or "").strip()
        message = str(job.get("message") or "").strip()
        if not schedule or not message:
            logger.warning(
                "cron_trigger.skip name=%s reason=missing_schedule_or_message",
                name,
            )
            continue
        urgency = str(job.get("urgency") or "normal").lower()
        trigger = CronTrigger(
            name=name,
            schedule_expr=schedule,
            message=message,
            urgency=urgency,
        )
        if trigger._next_fire_ts is None:
            # parse_schedule already logged; just skip the registration
            # so the trigger doesn't pollute trigger_names() as a no-op.
            continue
        out.append(trigger)
        seen_names.add(name)
    return out


__all__ = ["CronTrigger", "build_cron_triggers_from_config"]
