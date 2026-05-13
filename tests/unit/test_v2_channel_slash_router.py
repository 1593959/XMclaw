"""Sprint 2 Wave 10 — Slash command router unit tests.

Covers parsing, dispatch, side-effects (subscribe persistence),
read-only commands degrading gracefully when subsystems aren't wired,
and unknown-command behavior.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from xmclaw.daemon.channel_slash_router import (
    is_slash_command,
    route,
)


# ── parsing ───────────────────────────────────────────────────────


def test_is_slash_command_recognizes_slash_prefix() -> None:
    assert is_slash_command("/help") is True
    assert is_slash_command("  /状态  ") is True
    assert is_slash_command("/订阅 extra args") is True


def test_is_slash_command_rejects_non_slash() -> None:
    assert is_slash_command("hello") is False
    assert is_slash_command("") is False
    assert is_slash_command("/") is False                 # bare slash
    assert is_slash_command("https://example.com") is False


# ── /help ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_lists_all_commands() -> None:
    reply = await route(
        "/help",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_x",
    )
    # All canonical aliases should appear.
    for alias in ("/help", "/订阅", "/取消订阅", "/状态", "/日程", "/任务"):
        assert alias in reply


@pytest.mark.asyncio
async def test_chinese_alias_routes_to_help() -> None:
    reply = await route(
        "/帮助",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "命令" in reply


# ── /subscribe ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_persists_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "targets.json"
    monkeypatch.setattr(
        "xmclaw.cognition.proactive_target_store._default_path",
        lambda: store_path,
    )
    reply = await route(
        "/订阅",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_abc",
    )
    assert "已订阅" in reply
    # Second time is a noop with friendly message.
    reply2 = await route(
        "/订阅",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_abc",
    )
    assert "之前就订阅过了" in reply2


@pytest.mark.asyncio
async def test_unsubscribe_after_subscribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "targets.json"
    monkeypatch.setattr(
        "xmclaw.cognition.proactive_target_store._default_path",
        lambda: store_path,
    )
    await route("/订阅", app_state=SimpleNamespace(),
                channel="feishu", chat_ref="oc_x")
    reply = await route(
        "/取消订阅",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "已取消订阅" in reply


@pytest.mark.asyncio
async def test_unsubscribe_when_not_subscribed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "targets.json"
    monkeypatch.setattr(
        "xmclaw.cognition.proactive_target_store._default_path",
        lambda: store_path,
    )
    reply = await route(
        "/取消订阅",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_never_subscribed",
    )
    assert "本来就没订阅" in reply


# ── /status ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_with_minimal_state() -> None:
    """Even with NOTHING wired, /status must return something
    user-readable, not raise."""
    reply = await route(
        "/status",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "XMclaw 状态" in reply
    assert "运行时长" in reply


@pytest.mark.asyncio
async def test_status_reports_uptime_and_triggers() -> None:
    import time
    state = SimpleNamespace(
        boot_ts=time.time() - 65.0,  # 1 min ago
        proactive_agent=SimpleNamespace(
            trigger_names=lambda: ["idle_check_in", "calendar_reminder"],
        ),
    )
    reply = await route(
        "/状态",
        app_state=state,
        channel="feishu",
        chat_ref="oc_x",
    )
    # 65s → "1m" formatted
    assert "1m" in reply
    assert "idle_check_in" in reply
    assert "calendar_reminder" in reply


@pytest.mark.asyncio
async def test_status_reports_autobio_counts() -> None:
    autobio = MagicMock()
    autobio.people.return_value = [object(), object(), object()]
    autobio.projects.return_value = [object()]
    state = SimpleNamespace(autobio_memory=autobio)
    reply = await route(
        "/status",
        app_state=state,
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "3 个人" in reply
    assert "1 项目" in reply


# ── /calendar ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_missing_config() -> None:
    reply = await route(
        "/日程",
        app_state=SimpleNamespace(config={}),
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "未配置" in reply


@pytest.mark.asyncio
async def test_calendar_missing_file(tmp_path: Path) -> None:
    state = SimpleNamespace(
        config={
            "cognition": {
                "proactive": {
                    "calendar_ics_path": str(tmp_path / "nonexistent.ics"),
                },
            },
        },
    )
    reply = await route(
        "/日程",
        app_state=state,
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "不存在" in reply


@pytest.mark.asyncio
async def test_calendar_lists_upcoming(tmp_path: Path) -> None:
    import datetime as dt
    import time
    soon = time.time() + 120
    ics_str = dt.datetime.fromtimestamp(
        soon, tz=dt.timezone.utc,
    ).strftime("%Y%m%dT%H%M%SZ")
    ics_path = tmp_path / "cal.ics"
    ics_path.write_text(
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "UID:e1\n"
        "SUMMARY:测试会议\n"
        f"DTSTART:{ics_str}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n",
        encoding="utf-8",
    )
    state = SimpleNamespace(
        config={
            "cognition": {
                "proactive": {"calendar_ics_path": str(ics_path)},
            },
        },
    )
    reply = await route(
        "/日程",
        app_state=state,
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "测试会议" in reply
    assert "分钟后" in reply or "1 小时后" in reply


# ── /tasks ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tasks_without_cognition() -> None:
    reply = await route(
        "/tasks",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "认知架构未启用" in reply


@pytest.mark.asyncio
async def test_tasks_lists_goals() -> None:
    g1 = SimpleNamespace(
        priority=8, description="Wave 10 落地", status="active",
    )
    g2 = SimpleNamespace(
        priority=3, description="数据库迁移", status="pending",
    )
    state = SimpleNamespace(
        cognitive_state=SimpleNamespace(current_goals=[g1, g2]),
    )
    reply = await route(
        "/任务",
        app_state=state,
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "Wave 10 落地" in reply
    assert "数据库迁移" in reply
    assert "P8" in reply


# ── unknown command ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_command_returns_hint() -> None:
    reply = await route(
        "/nosuchcommand",
        app_state=SimpleNamespace(),
        channel="feishu",
        chat_ref="oc_x",
    )
    assert "未识别" in reply
    assert "/帮助" in reply
