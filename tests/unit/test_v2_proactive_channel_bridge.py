"""Sprint 2 Wave 9 — ProactiveChannelBridge unit tests.

The bridge subscribes to PROACTIVE_PROPOSAL events on the real
InProcessEventBus and forwards them as OutboundMessage to each
configured channel adapter. Tests cover:

  * Configured target receives proposal text
  * Urgency=high gets the 🚨 prefix
  * Urgency below min_urgency is filtered out
  * Non-PROACTIVE_PROPOSAL events are ignored (predicate)
  * One channel raising doesn't block others (parallel fan-out)
  * Empty messages are dropped
  * build_bridge_from_config wires multiple adapters from config dict
  * build_bridge_from_config returns None when no adapter opted in
"""
from __future__ import annotations

from typing import Any

import pytest

from xmclaw.cognition.proactive_channel_bridge import (
    ProactiveChannelBridge,
    build_bridge_from_config,
)
from xmclaw.core.bus import EventType, make_event
from xmclaw.core.bus.memory import InProcessEventBus


class _FakeAdapter:
    """Minimal ChannelAdapter stand-in. ``sends`` records every call."""

    def __init__(self, name: str = "feishu") -> None:
        self.name = name
        self.sends: list[tuple[str, str]] = []  # (target_ref, content)

    async def send(self, target: Any, payload: Any) -> str:
        self.sends.append((target.ref, payload.content))
        return "msg-123"


class _FlakyAdapter:
    """First send raises, subsequent ones succeed."""

    def __init__(self, name: str = "telegram") -> None:
        self.name = name
        self.sends: list[tuple[str, str]] = []
        self._fail_next = True

    async def send(self, target: Any, payload: Any) -> str:
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("api timeout")
        self.sends.append((target.ref, payload.content))
        return "msg-123"


async def _publish_proposal(
    bus: InProcessEventBus,
    *,
    message: str,
    urgency: str = "normal",
    trigger: str = "idle_check_in",
) -> None:
    ev = make_event(
        session_id="proactive",
        agent_id="proactive",
        type=EventType.PROACTIVE_PROPOSAL,
        payload={
            "trigger": trigger,
            "message": message,
            "urgency": urgency,
            "ts": 0.0,
        },
    )
    await bus.publish(ev)
    await bus.drain()


# ── Core fan-out behavior ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_forwards_normal_proposal_to_target():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "oc_test_chat")
    await bridge.start()

    await _publish_proposal(bus, message="2 分钟后会议：站会")

    assert len(adapter.sends) == 1
    target_ref, content = adapter.sends[0]
    assert target_ref == "oc_test_chat"
    assert content == "2 分钟后会议：站会"
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_prefixes_high_urgency_with_alert():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()

    await _publish_proposal(
        bus, message="服务器要爆了", urgency="high",
    )

    assert adapter.sends[0][1].startswith("🚨 ")
    assert "服务器要爆了" in adapter.sends[0][1]
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_drops_low_urgency_when_min_is_normal():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus, min_urgency="normal")
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()

    await _publish_proposal(
        bus, message="低优", urgency="low",
    )
    await _publish_proposal(
        bus, message="正常", urgency="normal",
    )

    # Only the normal one should land.
    assert len(adapter.sends) == 1
    assert adapter.sends[0][1] == "正常"
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_drops_normal_when_min_urgency_is_high():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus, min_urgency="high")
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()

    await _publish_proposal(
        bus, message="正常优先级别推", urgency="normal",
    )
    await _publish_proposal(
        bus, message="紧急要推", urgency="high",
    )

    assert len(adapter.sends) == 1
    assert "紧急要推" in adapter.sends[0][1]
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_skips_empty_messages():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()

    await _publish_proposal(bus, message="")
    await _publish_proposal(bus, message="   ")

    assert adapter.sends == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_ignores_non_proactive_events():
    """A USER_MESSAGE event should NOT trigger fan-out."""
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()

    other_ev = make_event(
        session_id="s1",
        agent_id="main",
        type=EventType.USER_MESSAGE,
        payload={"content": "hi"},
    )
    await bus.publish(other_ev)
    await bus.drain()

    assert adapter.sends == []
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_does_nothing_when_disabled():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus, enabled=False)
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()

    await _publish_proposal(bus, message="不应该送到")

    assert adapter.sends == []
    await bridge.stop()


# ── Resilience: one failing adapter doesn't block others ──────────


@pytest.mark.asyncio
async def test_bridge_resilient_to_one_adapter_failure():
    bus = InProcessEventBus()
    flaky = _FlakyAdapter(name="telegram")
    healthy = _FakeAdapter(name="feishu")
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(flaky, "tg_chat")
    bridge.add_target(healthy, "oc_chat")
    await bridge.start()

    await _publish_proposal(bus, message="测试一下")

    # Healthy adapter still got it.
    assert len(healthy.sends) == 1
    assert healthy.sends[0] == ("oc_chat", "测试一下")
    # Flaky adapter raised on first send → 0 records.
    assert flaky.sends == []
    await bridge.stop()


# ── Idempotent target registration ────────────────────────────────


def test_add_target_replaces_duplicate():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "oc_chat")
    bridge.add_target(adapter, "oc_chat")
    bridge.add_target(adapter, "oc_chat")
    assert bridge.target_count() == 1


def test_add_target_rejects_empty_ref():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "")
    bridge.add_target(adapter, None)  # type: ignore[arg-type]
    assert bridge.target_count() == 0


# ── build_bridge_from_config helper ────────────────────────────────


def test_build_bridge_from_config_wires_enabled_channels():
    bus = InProcessEventBus()
    feishu = _FakeAdapter(name="feishu")
    tg = _FakeAdapter(name="telegram")
    channels_cfg = {
        "feishu": {
            "enabled": True,
            "proactive_chat_id": "oc_abcd",
        },
        "telegram": {
            "enabled": True,
            "proactive_chat_id": "12345",
        },
        "slack": {
            "enabled": False,         # disabled
            "proactive_chat_id": "Cxyz",
        },
        "dingtalk": {
            "enabled": True,
            # no proactive_chat_id → skip
        },
    }
    bridge = build_bridge_from_config(
        bus=bus,
        channels_config=channels_cfg,
        proactive_push_config={"enabled": True},
        adapters=[feishu, tg],
    )
    assert bridge is not None
    assert bridge.target_count() == 2


def test_build_bridge_from_config_returns_none_when_no_opt_in():
    bus = InProcessEventBus()
    feishu = _FakeAdapter(name="feishu")
    bridge = build_bridge_from_config(
        bus=bus,
        channels_config={
            "feishu": {"enabled": True},  # no proactive_chat_id
        },
        proactive_push_config={"enabled": True},
        adapters=[feishu],
    )
    assert bridge is None


def test_build_bridge_from_config_honors_min_urgency():
    bus = InProcessEventBus()
    feishu = _FakeAdapter(name="feishu")
    bridge = build_bridge_from_config(
        bus=bus,
        channels_config={
            "feishu": {
                "enabled": True,
                "proactive_chat_id": "oc_x",
            },
        },
        proactive_push_config={"min_urgency": "high"},
        adapters=[feishu],
    )
    assert bridge is not None
    # Internal rank check — high == 2
    assert bridge._min_urgency_rank == 2


# ── Stop unsubscribes cleanly ─────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_stop_unsubscribes():
    bus = InProcessEventBus()
    adapter = _FakeAdapter()
    bridge = ProactiveChannelBridge(bus=bus)
    bridge.add_target(adapter, "oc_chat")
    await bridge.start()
    await bridge.stop()

    # After stop, new events should NOT route.
    await _publish_proposal(bus, message="不应该收到")
    assert adapter.sends == []
