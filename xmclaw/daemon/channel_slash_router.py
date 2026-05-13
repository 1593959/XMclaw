"""Slash-command router for IM channel inbound — Wave 10.

When a user's first character is ``/``, ChannelDispatcher diverts the
message here instead of running an AgentLoop turn. This is a cheap
way to give IM channels (飞书 / Telegram / Slack / …) a "control
panel" alongside the conversational interface.

Currently supported (both Chinese + English aliases):

  /help        /帮助       List all commands
  /subscribe   /订阅       Register current chat for proactive push
  /unsubscribe /取消订阅   Drop current chat from proactive push
  /status      /状态       Quick daemon health summary
  /calendar    /日程       Today's upcoming events from ICS (if configured)
  /tasks       /任务       Active goals from CognitiveState (if wired)

Unknown commands return a short hint pointing at /help — they do NOT
fall through to AgentLoop so a typo'd ``/foo`` doesn't burn an LLM
turn. Conventional messages (no leading ``/``) pass through untouched.

The router is intentionally *pure-ish*: it reads from app.state but
doesn't keep its own background state. Side effects (file writes for
/subscribe) live in dedicated stores so the router is easy to unit
test.
"""
from __future__ import annotations

import time
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


def is_slash_command(text: str) -> bool:
    """True if ``text`` is a /command we should handle."""
    s = (text or "").strip()
    return s.startswith("/") and len(s) > 1


def _parse(text: str) -> tuple[str, list[str]]:
    """Returns (verb_lowered, args_list) — verb without leading slash."""
    s = (text or "").strip()
    parts = s.split()
    verb = parts[0][1:].lower() if parts else ""
    args = parts[1:]
    return verb, args


# Canonical command id → (verb aliases, description).
_COMMANDS: list[tuple[str, tuple[str, ...], str]] = [
    ("help",        ("help", "帮助", "h", "?"),
        "列出所有可用命令"),
    ("subscribe",   ("subscribe", "订阅", "sub"),
        "把当前聊天注册成主动推送目标"),
    ("unsubscribe", ("unsubscribe", "取消订阅", "unsub"),
        "取消当前聊天的主动推送"),
    ("status",      ("status", "状态", "stat"),
        "守护进程 / 主动认知 / 记忆快照"),
    ("calendar",    ("calendar", "日程", "cal"),
        "今天的日历事件（需配 calendar_ics_path）"),
    ("tasks",       ("tasks", "任务", "task"),
        "当前活跃目标列表"),
]

_ALIAS_TO_ID = {
    alias: cmd_id
    for cmd_id, aliases, _ in _COMMANDS
    for alias in aliases
}


async def route(
    text: str,
    *,
    app_state: Any,
    channel: str,
    chat_ref: str,
) -> str:
    """Dispatch a slash command. Returns the reply text to send back
    to the channel. Caller guarantees ``is_slash_command(text)``."""
    verb, args = _parse(text)
    cmd_id = _ALIAS_TO_ID.get(verb)
    if cmd_id is None:
        return (
            f"未识别命令 `/{verb}`。\n"
            "发 `/帮助` 看所有命令。"
        )
    try:
        if cmd_id == "help":
            return _cmd_help()
        if cmd_id == "subscribe":
            return await _cmd_subscribe(channel, chat_ref)
        if cmd_id == "unsubscribe":
            return await _cmd_unsubscribe(channel, chat_ref)
        if cmd_id == "status":
            return _cmd_status(app_state)
        if cmd_id == "calendar":
            return _cmd_calendar(app_state)
        if cmd_id == "tasks":
            return _cmd_tasks(app_state)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "slash.cmd_failed cmd=%s err=%s", cmd_id, exc,
        )
        return f"命令出错：{exc}"
    return f"命令 `/{verb}` 暂未实现。"


# ── Commands ───────────────────────────────────────────────────────


def _cmd_help() -> str:
    lines = ["**XMclaw 控制台命令**"]
    for cmd_id, aliases, desc in _COMMANDS:
        alias_str = " / ".join(f"/{a}" for a in aliases[:3])
        lines.append(f"- {alias_str} — {desc}")
    return "\n".join(lines)


async def _cmd_subscribe(channel: str, chat_ref: str) -> str:
    from xmclaw.cognition.proactive_target_store import add_target
    added = await add_target(channel, chat_ref)
    if added:
        return (
            "✅ 已订阅。以后日历提醒 / 闲置提醒 / 主动建议都会推到这里。\n"
            "想停就发 `/取消订阅`。"
        )
    return "ℹ️ 这个聊天之前就订阅过了，没重复添加。"


async def _cmd_unsubscribe(channel: str, chat_ref: str) -> str:
    from xmclaw.cognition.proactive_target_store import remove_target
    removed = await remove_target(channel, chat_ref)
    if removed:
        return "✅ 已取消订阅。主动推送不会再发到这里。"
    return "ℹ️ 这个聊天本来就没订阅。"


def _cmd_status(app_state: Any) -> str:
    boot_ts = getattr(app_state, "boot_ts", None)
    uptime_line = "—"
    if boot_ts:
        secs = max(0, int(time.time() - float(boot_ts)))
        if secs < 60:
            uptime_line = f"{secs}s"
        elif secs < 3600:
            uptime_line = f"{secs // 60}m"
        else:
            hr = secs // 3600
            uptime_line = f"{hr}h {(secs % 3600) // 60}m"

    pa = getattr(app_state, "proactive_agent", None)
    triggers_line = "—"
    if pa is not None:
        try:
            names = pa.trigger_names()
            triggers_line = f"{len(names)} 个：{', '.join(names)}"
        except Exception:  # noqa: BLE001
            pass

    autobio = getattr(app_state, "autobio_memory", None)
    autobio_line = "—"
    if autobio is not None:
        try:
            n_people = len(autobio.people(limit=200))
            n_proj = len(autobio.projects(limit=200))
            autobio_line = f"{n_people} 个人 / {n_proj} 项目"
        except Exception:  # noqa: BLE001
            pass

    return (
        "**XMclaw 状态**\n"
        f"- 运行时长：{uptime_line}\n"
        f"- 主动触发器：{triggers_line}\n"
        f"- 自传式记忆：{autobio_line}"
    )


def _cmd_calendar(app_state: Any) -> str:
    """Read configured ICS file (if any), filter to today, list."""
    config = getattr(app_state, "config", None) or {}
    proactive_cfg = (
        (config.get("cognition") or {}).get("proactive", {})
        if isinstance(config, dict) else {}
    )
    ics_path = (
        proactive_cfg.get("calendar_ics_path")
        if isinstance(proactive_cfg, dict) else None
    )
    if not isinstance(ics_path, str) or not ics_path.strip():
        return (
            "ℹ️ 未配置 `cognition.proactive.calendar_ics_path`，"
            "无法读取日程。"
        )
    try:
        from pathlib import Path

        from xmclaw.cognition.triggers_environment import _parse_ics
        text = Path(ics_path.strip()).expanduser().read_text(
            encoding="utf-8",
        )
        events = _parse_ics(text)
    except FileNotFoundError:
        return f"⚠️ ICS 文件不存在：`{ics_path}`"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 读取 ICS 失败：{exc}"
    now = time.time()
    end_of_day = now + 24 * 3600
    upcoming = [
        e for e in events
        if now <= e.dtstart <= end_of_day
    ]
    if not upcoming:
        return "今天往后 24 小时没有日程。"
    upcoming.sort(key=lambda e: e.dtstart)
    lines = [f"**今天/明天的 {len(upcoming)} 个日程**"]
    for e in upcoming[:10]:
        mins = max(0, int((e.dtstart - now) / 60))
        if mins < 60:
            when = f"{mins} 分钟后"
        elif mins < 1440:
            when = f"{mins // 60} 小时后"
        else:
            when = f"明天 {(mins - 1440) // 60} 小时后"
        loc = f"（{e.location}）" if e.location else ""
        lines.append(f"- ⏰ {when}：{e.summary}{loc}")
    return "\n".join(lines)


def _cmd_tasks(app_state: Any) -> str:
    cs = getattr(app_state, "cognitive_state", None)
    if cs is None:
        agent = getattr(app_state, "agent", None)
        cs = getattr(agent, "_cognitive_state", None) if agent else None
    if cs is None:
        return "ℹ️ 认知架构未启用，没有任务列表。"
    try:
        goals = list(getattr(cs, "current_goals", []))
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 读取目标失败：{exc}"
    if not goals:
        return "当前没有活跃目标。"
    lines = [f"**当前 {len(goals)} 个目标**"]
    for g in goals[:10]:
        prio = getattr(g, "priority", "?")
        desc = getattr(g, "description", "?")
        status = getattr(g, "status", "?")
        lines.append(f"- P{prio} [{status}] {desc}")
    return "\n".join(lines)


__all__ = ["is_slash_command", "route"]
