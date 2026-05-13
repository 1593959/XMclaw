"""Sprint 2 Wave 18 — per-user session partitioning tests.

Same group chat, two different senders → two different session_ids
when session_per_user is on for that channel. Single-channel
(legacy) behavior preserved when the flag is off.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xmclaw.daemon.channel_dispatcher import ChannelDispatcher
from xmclaw.providers.channel.base import (
    ChannelTarget,
    InboundMessage,
)


@pytest.fixture
def fake_agent() -> MagicMock:
    a = MagicMock()
    a.run_turn = AsyncMock()
    a._histories = {}
    return a


def _msg(channel: str, ref: str, user_ref: str, content: str = "hi"):
    return InboundMessage(
        target=ChannelTarget(channel=channel, ref=ref),
        user_ref=user_ref,
        content=content,
        raw={"message_id": "m1"},
    )


# ── default (legacy) ─────────────────────────────────────────────


def test_default_behavior_shares_session_per_chat(
    fake_agent: MagicMock,
) -> None:
    """No session_per_user flag → all senders in one chat share
    one session_id (legacy behavior, preserves history)."""
    d = ChannelDispatcher(fake_agent)
    sid_alice = d._session_id_for(_msg("feishu", "oc_chat", "ou_alice"))
    sid_bob = d._session_id_for(_msg("feishu", "oc_chat", "ou_bob"))
    assert sid_alice == sid_bob == "feishu:oc_chat"


# ── opt-in per-user partitioning ─────────────────────────────────


def test_per_user_partitioning_splits_senders(
    fake_agent: MagicMock,
) -> None:
    d = ChannelDispatcher(
        fake_agent,
        session_per_user_channels=frozenset(["feishu"]),
    )
    sid_alice = d._session_id_for(_msg("feishu", "oc_chat", "ou_alice"))
    sid_bob = d._session_id_for(_msg("feishu", "oc_chat", "ou_bob"))
    assert sid_alice != sid_bob
    assert sid_alice == "feishu:oc_chat:ou_alice"
    assert sid_bob == "feishu:oc_chat:ou_bob"


def test_per_user_partitioning_per_channel_only(
    fake_agent: MagicMock,
) -> None:
    """The flag is per-channel — telegram in the same dispatcher
    keeps legacy behavior unless explicitly enabled."""
    d = ChannelDispatcher(
        fake_agent,
        session_per_user_channels=frozenset(["feishu"]),
    )
    sid_tg = d._session_id_for(_msg("telegram", "12345", "user_1"))
    assert sid_tg == "telegram:12345"


def test_per_user_blank_user_ref_falls_back_to_unknown(
    fake_agent: MagicMock,
) -> None:
    """A message with empty/None user_ref shouldn't crash — falls
    back to a stable 'unknown' bucket so all anonymous senders
    end up together."""
    d = ChannelDispatcher(
        fake_agent,
        session_per_user_channels=frozenset(["feishu"]),
    )
    sid = d._session_id_for(_msg("feishu", "oc_chat", ""))
    assert sid == "feishu:oc_chat:unknown"


def test_per_user_strips_whitespace_in_user_ref(
    fake_agent: MagicMock,
) -> None:
    d = ChannelDispatcher(
        fake_agent,
        session_per_user_channels=frozenset(["feishu"]),
    )
    sid = d._session_id_for(_msg("feishu", "oc_chat", "  ou_x  "))
    assert sid == "feishu:oc_chat:ou_x"


def test_partition_persists_across_messages_same_user(
    fake_agent: MagicMock,
) -> None:
    """Stability: same user in same chat across many messages →
    always the same session_id (so conversation history sticks)."""
    d = ChannelDispatcher(
        fake_agent,
        session_per_user_channels=frozenset(["feishu"]),
    )
    sids = {
        d._session_id_for(_msg("feishu", "oc_chat", "ou_alice", str(i)))
        for i in range(20)
    }
    assert len(sids) == 1
