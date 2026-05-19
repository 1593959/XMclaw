"""DailyDigestTrigger — 每日活动汇总 (Wave 16).

CronTrigger 触发预设文本；这个触发器到点动态构建当天的活动总结：

  * N 条 ProactiveAgent 主动发声（按 trigger 名聚合）
  * 反思周期 / 元认知 / 任务状态变化的次数
  * 自传记忆有没有新增
  * 当前活跃目标数量

输出一段 markdown 推到 PROACTIVE_PROPOSAL，Web UI 看得到，Wave 9
的 channel_bridge 还会把它推到飞书。

为什么不复用 CronTrigger：CronTrigger 的 message 是固定字符串；
digest 需要"到点了再扫 bus + autobio 算总结"，所以 propose 时
要做实际工作。

实现复用 Wave 8 dashboard router 里 _summarize_event 的命名约定，
保持事件 emoji 一致（user 在 dashboard 时间线和 digest 里看到的是
同一种描述）。
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

# Match Wave 8 dashboard timeline filter for consistency.
_TIMELINE_EVENT_TYPES = (
    "proactive_proposal",
    "reflection_cycle_ran",
    "memory_consolidated",
    "goals_groomed",
    "metacognition_proposal",
    "task_state_changed",
    "evolution_promoted",
)


class DailyDigestTrigger(ProactiveTrigger):
    """Fires at ``schedule_expr`` (e.g. "0 22 * * *") with a built-up
    summary of the past ``lookback_h`` hours.

    Same advance-then-build pattern as CronTrigger — schedule slot is
    advanced on propose so a crash doesn't loop.
    """

    def __init__(
        self,
        *,
        bus: Any,
        schedule_expr: str = "0 22 * * *",
        lookback_h: float = 24.0,
        urgency: str = "normal",
        agent_loop: Any = None,
    ) -> None:
        self.name = "daily_digest"
        # Slightly higher than the default cooldown so a flapping run
        # can't double-fire within minutes of the slot.
        self.cooldown_s = 600.0
        self._bus = bus
        self._schedule_expr = schedule_expr
        self._lookback_s = float(lookback_h) * 3600.0
        self._urgency = (
            urgency if urgency in ("low", "normal", "high") else "normal"
        )
        self._agent_loop = agent_loop
        # Epic #27 sweep #15 (2026-05-19): pre-fix when cron parsing
        # failed (croniter not installed, daemon never reloaded
        # after pyproject update, etc.) we set ``_next_fire_ts =
        # None`` and ``should_fire`` always returned False ⇒
        # daily_digest effectively never ran. daemon.log on the
        # user's machine showed 47 ``bad_schedule`` warnings ÷
        # the trigger tick frequency = the feature has been silently
        # dead since install. Now: fall back to a 24h interval when
        # cron parsing fails so the digest still fires once a day,
        # and surface the fallback so operators can fix at their
        # leisure (the digest still works in the meantime).
        self._used_interval_fallback = False
        try:
            self._next_fire_ts: float | None = parse_schedule(
                schedule_expr, now=time.time(),
            )
        except ValueError as exc:
            try:
                self._next_fire_ts = parse_schedule(
                    "every 1d", now=time.time(),
                )
                self._used_interval_fallback = True
                logger.warning(
                    "daily_digest.cron_unavailable expr=%r err=%s — "
                    "falling back to 'every 1d' interval. To restore "
                    "the configured time-of-day, run "
                    "``pip install croniter>=2.0.0`` and restart the "
                    "daemon.",
                    schedule_expr, exc,
                )
            except ValueError as exc2:
                logger.warning(
                    "daily_digest.bad_schedule expr=%r err=%s "
                    "fallback_err=%s",
                    schedule_expr, exc, exc2,
                )
                self._next_fire_ts = None

    async def should_fire(self, ctx: ProactiveContext) -> bool:
        if self._next_fire_ts is None:
            return False
        return ctx.now >= self._next_fire_ts

    async def propose(
        self, ctx: ProactiveContext,
    ) -> TriggerProposal | None:
        if self._next_fire_ts is None or ctx.now < self._next_fire_ts:
            return None
        # Epic #27 sweep #15: re-schedule honours the same fallback
        # logic as the constructor so a recurring digest keeps
        # firing once a day even on a croniter-less daemon.
        next_expr = (
            "every 1d" if self._used_interval_fallback
            else self._schedule_expr
        )
        try:
            self._next_fire_ts = parse_schedule(
                next_expr, now=ctx.now + 1.0,
            )
        except ValueError:
            self._next_fire_ts = None

        message = self._build_digest(ctx.now)
        return TriggerProposal(
            trigger_name=self.name,
            message=message,
            urgency=self._urgency,
            payload={
                "schedule": self._schedule_expr,
                "lookback_h": self._lookback_s / 3600.0,
            },
        )

    def _build_digest(self, now: float) -> str:
        """Stitch the markdown summary. Each section is independently
        try/except so one failing query doesn't tank the whole digest."""
        lines: list[str] = ["📔 **今日活动汇总**"]

        # ── Section 1: proactive + cognition events from event bus ──
        events_section = self._summarize_events(now)
        if events_section:
            lines.append("")
            lines.append(events_section)

        # ── Section 2: autobio memory snapshot ──
        autobio_section = self._summarize_autobio()
        if autobio_section:
            lines.append("")
            lines.append(autobio_section)

        # ── Section 3: active goals ──
        goals_section = self._summarize_goals()
        if goals_section:
            lines.append("")
            lines.append(goals_section)

        return "\n".join(lines)

    def _summarize_events(self, now: float) -> str:
        bus = self._bus
        query = getattr(bus, "query", None) if bus else None
        if not callable(query):
            return ""
        try:
            evs = query(
                since=now - self._lookback_s,
                types=list(_TIMELINE_EVENT_TYPES),
                limit=500,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "daily_digest.query_failed err=%s", exc,
            )
            return ""
        if not evs:
            return "## 今天没有主动认知活动"
        counts: dict[str, int] = {}
        for e in evs:
            t = getattr(e, "type", "")
            if hasattr(t, "value"):
                t = t.value
            counts[str(t)] = counts.get(str(t), 0) + 1
        lines = ["## 主动认知"]
        emoji = {
            "proactive_proposal":     "📢 主动发声",
            "reflection_cycle_ran":   "🪞 反思周期",
            "memory_consolidated":    "🧠 记忆整理",
            "goals_groomed":          "🎯 目标梳理",
            "metacognition_proposal": "💡 元认知建议",
            "task_state_changed":     "🔄 任务状态变化",
            "evolution_promoted":     "⬆ 技能晋升",
        }
        # Stable order: proactive first, then by count desc.
        sorted_counts = sorted(
            counts.items(),
            key=lambda kv: (
                0 if kv[0] == "proactive_proposal" else 1,
                -kv[1],
            ),
        )
        for k, c in sorted_counts:
            lines.append(f"- {emoji.get(k, k)}: {c} 次")
        return "\n".join(lines)

    def _summarize_autobio(self) -> str:
        if self._agent_loop is None:
            return ""
        autobio = getattr(self._agent_loop, "_autobio_memory", None)
        if autobio is None:
            return ""
        try:
            people = autobio.people(limit=200)
            projects = autobio.projects(limit=200)
        except Exception:  # noqa: BLE001
            return ""
        if not people and not projects:
            return ""
        return (
            "## 自传记忆\n"
            f"- 记得 {len(people)} 个人 / {len(projects)} 个项目"
        )

    def _summarize_goals(self) -> str:
        if self._agent_loop is None:
            return ""
        cs = getattr(self._agent_loop, "_cognitive_state", None)
        if cs is None:
            return ""
        try:
            goals = list(getattr(cs, "current_goals", []) or [])
        except Exception:  # noqa: BLE001
            return ""
        if not goals:
            return ""
        lines = [f"## 还在路上的目标（{len(goals)}）"]
        for g in goals[:5]:
            prio = getattr(g, "priority", "?")
            desc = getattr(g, "description", "?")
            status = getattr(g, "status", "?")
            lines.append(f"- P{prio} [{status}] {desc}")
        return "\n".join(lines)


__all__ = ["DailyDigestTrigger"]
