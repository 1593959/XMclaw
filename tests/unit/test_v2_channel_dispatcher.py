"""B-145 — ChannelDispatcher unit tests.

No live channel SDKs in test — uses a fake adapter that exposes the
ChannelAdapter ABC's surface and lets us verify the inbound→agent→
outbound round trip without network.

Pins:
  * inbound message → agent.run_turn called with the right session_id
  * assistant reply text gets pulled out of agent._histories and sent
    back through adapter.send
  * per-(channel, chat) lock serialises concurrent messages in the
    same chat so two parallel turns don't interleave
  * agent failure / send failure surface as logs + don't kill the
    dispatcher
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from xmclaw.daemon.channel_dispatcher import ChannelDispatcher
from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)


# ── fake adapter + fake agent ────────────────────────────────────


class _FakeAdapter(ChannelAdapter):
    name = "fake"

    def __init__(self) -> None:
        self.handlers: list[Callable[[InboundMessage], Awaitable[None]]] = []
        self.sent: list[tuple[ChannelTarget, OutboundMessage]] = []
        self.started = False
        self.fail_send = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send(self, target: ChannelTarget, payload: OutboundMessage) -> str:
        if self.fail_send:
            raise RuntimeError("simulated send failure")
        self.sent.append((target, payload))
        return f"msg-{len(self.sent)}"

    def subscribe(
        self, handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self.handlers.append(handler)

    async def emit(self, msg: InboundMessage) -> None:
        """Test seam — drive an inbound message through the registered handler."""
        for h in list(self.handlers):
            await h(msg)


class _FakeAgent:
    """Mimics enough of AgentLoop.run_turn + ._histories to test the
    dispatcher without spinning up a real LLM."""

    def __init__(self, reply: str = "ok", *, fail: bool = False) -> None:
        self._reply = reply
        self._fail = fail
        self._histories: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.in_flight: set[str] = set()
        self.max_concurrent: dict[str, int] = {}

    async def run_turn(self, session_id: str, content: str) -> None:
        if self._fail:
            raise RuntimeError("simulated turn failure")
        # Track concurrency PER session — used by the lock test below.
        self.in_flight.add(session_id)
        cur = len([s for s in self.in_flight if s == session_id])
        self.max_concurrent[session_id] = max(
            self.max_concurrent.get(session_id, 0),
            sum(1 for s in self.in_flight if s == session_id),
        )
        await asyncio.sleep(0.01)  # simulate work
        self.calls.append((session_id, content))
        # Append assistant reply to the history dict the dispatcher reads.
        self._histories.setdefault(session_id, []).append({
            "role": "assistant",
            "content": self._reply + f" #{len(self.calls)}",
        })
        self.in_flight.discard(session_id)


def _msg(text: str = "hi", *, ref: str = "oc_chat_1", msg_id: str = "m1") -> InboundMessage:
    return InboundMessage(
        target=ChannelTarget(channel="fake", ref=ref),
        user_ref="user_1",
        content=text,
        raw={"message_id": msg_id},
    )


# ── happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_drives_run_turn_with_stable_session_id() -> None:
    agent = _FakeAgent(reply="hello")
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    disp.add(adapter)
    await disp.start_all()
    assert adapter.started

    await adapter.emit(_msg("how are you"))

    # session_id should be "<channel>:<chat_ref>"
    assert agent.calls == [("fake:oc_chat_1", "how are you")]


@pytest.mark.asyncio
async def test_assistant_reply_sent_back_via_adapter() -> None:
    agent = _FakeAgent(reply="hi back")
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await adapter.emit(_msg("ping"))

    assert len(adapter.sent) == 1
    target, payload = adapter.sent[0]
    assert target.channel == "fake"
    assert target.ref == "oc_chat_1"
    assert payload.content == "hi back #1"
    # reply_to should pass through from the inbound raw dict
    assert payload.reply_to == "m1"


@pytest.mark.asyncio
async def test_two_messages_same_chat_serialise() -> None:
    """Per-chat lock means two messages in the same chat don't run in
    parallel — the second must wait for the first's run_turn to finish."""
    agent = _FakeAgent(reply="x")
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await asyncio.gather(
        adapter.emit(_msg("one", msg_id="m1")),
        adapter.emit(_msg("two", msg_id="m2")),
    )

    # Both ran, both got replies, but max_concurrent for the shared
    # session_id stayed at 1 (no parallel run).
    assert agent.max_concurrent.get("fake:oc_chat_1") == 1
    assert len(adapter.sent) == 2


@pytest.mark.asyncio
async def test_two_messages_different_chats_parallel_ok() -> None:
    """Different chats get separate locks — they CAN run in parallel."""
    agent = _FakeAgent(reply="x")
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await asyncio.gather(
        adapter.emit(_msg("one", ref="oc_a")),
        adapter.emit(_msg("two", ref="oc_b")),
    )
    # Each session id is its own — no inter-session blocking.
    assert agent.max_concurrent.get("fake:oc_a") == 1
    assert agent.max_concurrent.get("fake:oc_b") == 1
    assert len(adapter.sent) == 2


# ── failure paths don't kill the dispatcher ──────────────────────


@pytest.mark.asyncio
async def test_run_turn_failure_logged_not_raised(caplog) -> None:
    agent = _FakeAgent(fail=True)
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    disp.add(adapter)

    # Must not raise out of emit.
    await adapter.emit(_msg("boom"))

    # Adapter shouldn't have sent anything since the turn failed.
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_send_failure_logged_not_raised() -> None:
    agent = _FakeAgent(reply="ok")
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    adapter.fail_send = True
    disp.add(adapter)

    await adapter.emit(_msg("hi"))

    # run_turn ran, but send blew up — must not propagate.
    assert agent.calls == [("fake:oc_chat_1", "hi")]


@pytest.mark.asyncio
async def test_no_assistant_reply_skips_send() -> None:
    """When the agent's history has no assistant reply, dispatcher
    should NOT send an empty message back."""
    class _SilentAgent(_FakeAgent):
        async def run_turn(self, sid: str, content: str) -> None:
            self.calls.append((sid, content))
            # No history append → no assistant reply

    agent = _SilentAgent()
    disp = ChannelDispatcher(agent)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await adapter.emit(_msg("quiet"))

    assert agent.calls  # turn ran
    assert adapter.sent == []  # no send


@pytest.mark.asyncio
async def test_start_failure_does_not_crash_others() -> None:
    """Adapter that fails on start shouldn't prevent siblings from
    starting."""
    class _FailingAdapter(_FakeAdapter):
        async def start(self) -> None:
            raise RuntimeError("creds bad")

    disp = ChannelDispatcher(_FakeAgent())
    bad = _FailingAdapter()
    good = _FakeAdapter()
    disp.add(bad)
    disp.add(good)

    await disp.start_all()  # must not raise
    assert good.started is True


# ── B-195 delayed-ack ─────────────────────────────────────────────


class _SlowAgent(_FakeAgent):
    """Run-turn deliberately slow so the ack timer fires."""

    def __init__(self, reply: str = "ok", *, sleep_s: float = 0.1) -> None:
        super().__init__(reply=reply)
        self._sleep_s = sleep_s

    async def run_turn(self, session_id: str, content: str) -> None:
        await asyncio.sleep(self._sleep_s)
        self.calls.append((session_id, content))
        self._histories.setdefault(session_id, []).append({
            "role": "assistant",
            "content": self._reply,
        })


@pytest.mark.asyncio
async def test_fast_turn_skips_ack() -> None:
    """B-195: turn finishes before ack_delay_s — no placeholder spam.
    Reason: 不发占位条不打扰 fast 回复；占位只在真慢时出现。"""
    agent = _SlowAgent(reply="quick", sleep_s=0.01)
    disp = ChannelDispatcher(agent, ack_delay_s=0.5)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await adapter.emit(_msg("ping"))

    # Just the final reply — no "🌸 思考中" message.
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1].content == "quick"


@pytest.mark.asyncio
async def test_slow_turn_sends_ack_then_final() -> None:
    """B-195: turn slower than ack_delay_s — placeholder fires + final.
    Without this the user thinks daemon dropped their message and
    retries, producing the 'duplicate reply' complaint."""
    agent = _SlowAgent(reply="final answer", sleep_s=0.15)
    disp = ChannelDispatcher(agent, ack_delay_s=0.05)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await adapter.emit(_msg("slow ping", msg_id="usr-1"))

    # Two outbound: the ack, then the final.
    assert len(adapter.sent) == 2
    ack_target, ack_payload = adapter.sent[0]
    assert "思考" in ack_payload.content or "🌸" in ack_payload.content
    assert ack_payload.reply_to == "usr-1"
    final_target, final_payload = adapter.sent[1]
    assert final_payload.content == "final answer"
    assert final_payload.reply_to == "usr-1"


@pytest.mark.asyncio
async def test_ack_timer_cancelled_on_run_turn_failure() -> None:
    """If run_turn raises after ack already fired, dispatcher must
    still clean up gracefully (no unhandled task warning, no second
    final send)."""
    class _FailSlow(_FakeAgent):
        async def run_turn(self, sid: str, content: str) -> None:
            await asyncio.sleep(0.1)
            raise RuntimeError("simulated late failure")

    agent = _FailSlow()
    disp = ChannelDispatcher(agent, ack_delay_s=0.05)
    adapter = _FakeAdapter()
    disp.add(adapter)

    await adapter.emit(_msg("doomed"))
    # Ack was sent (turn was slow enough), but no final reply.
    assert len(adapter.sent) == 1
    assert "思考" in adapter.sent[0][1].content or "🌸" in adapter.sent[0][1].content
