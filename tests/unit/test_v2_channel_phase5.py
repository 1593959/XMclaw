"""Tests for Phase 5 — channel plugin contract + registry + queue + tunnel."""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.providers.channel.base import (
    ChannelTarget,
    InboundMessage,
    PluginManifest,
)
from xmclaw.providers.channel.queue import UnifiedInboundQueue
from xmclaw.providers.channel.registry import (
    CHANNEL_IDS,
    discover,
    needs_tunnel,
)


# ── Manifests + registry ─────────────────────────────────────────────


def test_canonical_ids_match_dev_plan():
    # Phase 5 ports the 5 channels from QwenPaw docs/DEV_PLAN.md §1.6.
    assert CHANNEL_IDS == ("feishu", "dingtalk", "wecom", "weixin", "telegram")


def test_discover_returns_all_five_manifests():
    # B-38: default discover() filters scaffolds; pass
    # ``include_scaffolds=True`` to get the full Phase 5 set. All five
    # are scaffold-only as of B-37 (no adapter modules wired yet).
    manifests = discover(include_scaffolds=True)
    assert set(manifests.keys()) == set(CHANNEL_IDS)
    for cid, m in manifests.items():
        assert isinstance(m, PluginManifest)
        assert m.id == cid
        assert m.label  # human-readable
        assert m.adapter_factory_path  # importable path
        assert isinstance(m.requires, tuple)
        assert isinstance(m.config_schema, dict)


def test_default_discover_filters_scaffolds():
    """B-38: Phantom channel filter — scaffold-only manifests should
    NOT show up in default discover().

    B-145 update: feishu is now ``ready`` (real adapter at
    feishu/adapter.py), so default discover() returns {feishu}. The
    other 4 (dingtalk/wecom/weixin/telegram) remain scaffold and
    stay filtered out."""
    ready = discover()
    assert set(ready.keys()) == {"feishu"}
    # Sanity: feishu manifest carries the ready flag
    assert ready["feishu"].implementation_status == "ready"


def test_feishu_does_not_need_tunnel():
    m = discover(include_scaffolds=True)["feishu"]
    assert m.needs_tunnel is False  # lark-oapi WS long-poll


def test_wecom_needs_tunnel():
    m = discover(include_scaffolds=True)["wecom"]
    assert m.needs_tunnel is True


def test_needs_tunnel_aggregator():
    assert needs_tunnel(["feishu"]) is False
    assert needs_tunnel(["feishu", "telegram"]) is False
    assert needs_tunnel(["feishu", "wecom"]) is True
    assert needs_tunnel([]) is False


# ── UnifiedInboundQueue ──────────────────────────────────────────────


def _msg(channel: str, content: str = "hi") -> InboundMessage:
    return InboundMessage(
        target=ChannelTarget(channel=channel, ref="room-1"),
        user_ref="u-123",
        content=content,
    )


@pytest.mark.asyncio
async def test_queue_basic_put_get():
    q = UnifiedInboundQueue()
    assert q.size == 0
    await q.put(_msg("feishu"))
    await q.put(_msg("telegram"))
    assert q.size == 2
    a = await q.get()
    b = await q.get()
    assert a.target.channel == "feishu"
    assert b.target.channel == "telegram"
    assert q.size == 0


@pytest.mark.asyncio
async def test_queue_full_raises():
    q = UnifiedInboundQueue(maxsize=2)
    await q.put(_msg("feishu"))
    await q.put(_msg("dingtalk"))
    assert q.is_full
    with pytest.raises(asyncio.QueueFull):
        await q.put(_msg("wecom"))


@pytest.mark.asyncio
async def test_queue_drain_iterator():
    q = UnifiedInboundQueue()
    for i in range(3):
        await q.put(_msg(f"ch-{i}"))

    received: list[str] = []

    async def consumer():
        async for m in q.drain():
            received.append(m.target.channel)
            if len(received) == 3:
                await q.close()
                return

    await asyncio.wait_for(consumer(), timeout=1.0)
    assert received == ["ch-0", "ch-1", "ch-2"]


@pytest.mark.asyncio
async def test_queue_close_blocks_put():
    q = UnifiedInboundQueue()
    await q.close()
    with pytest.raises(RuntimeError, match="closed"):
        await q.put(_msg("feishu"))


# ── Tunnel availability check ────────────────────────────────────────


def test_is_cloudflared_available_returns_bool():
    from xmclaw.utils.tunnel import is_cloudflared_available

    out = is_cloudflared_available()
    assert isinstance(out, bool)
    # Don't assert True/False — depends on test host. Just verify the
    # function returns without crash.


def test_tunnel_manager_url_pattern():
    from xmclaw.utils.tunnel import _TUNNEL_URL_RE

    sample = "2026-04-26T00:00:00Z INF |  Visit it at: https://abc-def-ghi.trycloudflare.com  |"
    match = _TUNNEL_URL_RE.search(sample)
    assert match is not None
    assert match.group(0) == "https://abc-def-ghi.trycloudflare.com"
