"""AgentLoop recently-finished runs buffer — Wave-32+ (2026-05-18).

Pin the post-completion visibility behavior: after run_turn ends,
the buffer captures the last assistant message + elapsed time + ok
flag, so /api/v2/agent_tasks can render the entry as a "done" row
in the 后台任务 panel. Expires after 10 minutes.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Pricing


# Stub LLM that just replies "ok response".
class _StubLLM(LLMProvider):
    async def complete(self, messages, tools=None):
        return LLMResponse(content="ok response", tool_calls=())

    def stream(self, messages, tools=None, *, cancel=None):  # pragma: no cover
        raise NotImplementedError

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.mark.asyncio
async def test_finished_run_captures_reply_preview() -> None:
    """After run_turn returns, the buffer has one entry with the
    final assistant text + elapsed seconds."""
    agent = AgentLoop(llm=_StubLLM(), bus=InProcessEventBus())
    before = agent.list_recently_finished()
    assert before == []
    await agent.run_turn("sess-1", "hi")
    after = agent.list_recently_finished()
    assert len(after) == 1
    entry = after[0]
    assert entry["session_id"] == "sess-1"
    assert entry["ok"] is True
    assert "ok response" in entry["reply_preview"]
    assert entry["elapsed_s"] >= 0
    assert entry["user_message_preview"] == "hi"


@pytest.mark.asyncio
async def test_finished_runs_accumulate_across_turns() -> None:
    agent = AgentLoop(llm=_StubLLM(), bus=InProcessEventBus())
    await agent.run_turn("sess-a", "first")
    await agent.run_turn("sess-b", "second")
    rows = agent.list_recently_finished()
    sids = [r["session_id"] for r in rows]
    assert "sess-a" in sids
    assert "sess-b" in sids


@pytest.mark.asyncio
async def test_finished_runs_expire_after_ttl() -> None:
    """Entries older than _FINISHED_TTL_S are dropped on next read.
    Patch the entry's timestamp to simulate elapsed time without
    sleeping in the test."""
    agent = AgentLoop(llm=_StubLLM(), bus=InProcessEventBus())
    await agent.run_turn("sess-old", "msg")
    # Manually age the entry past the TTL.
    agent._recently_finished_runs[0]["finished_at"] = _time.time() - (agent._FINISHED_TTL_S + 1)
    rows = agent.list_recently_finished()
    assert rows == []


@pytest.mark.asyncio
async def test_finished_runs_bounded_by_cap() -> None:
    """Adding more than _FINISHED_BUFFER_CAP entries drops the oldest.
    Pin the bound so a tight retry loop can't OOM the daemon."""
    agent = AgentLoop(llm=_StubLLM(), bus=InProcessEventBus())
    cap = agent._FINISHED_BUFFER_CAP
    # Synthetically push cap + 5 entries via the internal recorder.
    for i in range(cap + 5):
        agent._record_finished_run(
            session_id=f"s{i}", started_at=_time.time() - 1,
            result=None, user_message=f"m{i}",
        )
    assert len(agent._recently_finished_runs) == cap
    # The oldest should be gone — entries with idx < 5 dropped.
    sids = {e["session_id"] for e in agent._recently_finished_runs}
    assert "s0" not in sids
    assert f"s{cap + 4}" in sids


@pytest.mark.asyncio
async def test_finished_run_error_path_captured() -> None:
    """If run_turn raises mid-flight, the buffer should STILL get a
    row (with ok=False) so the user sees the failure happened. The
    finally block runs regardless of return path."""
    @dataclass
    class _BrokenLLM(LLMProvider):
        async def complete(self, messages, tools=None):
            raise RuntimeError("kaboom")

        def stream(self, *a, **kw):  # pragma: no cover
            raise NotImplementedError

        @property
        def tool_call_shape(self):
            return ToolCallShape.ANTHROPIC_NATIVE

        @property
        def pricing(self):
            return Pricing()

    agent = AgentLoop(llm=_BrokenLLM(), bus=InProcessEventBus())
    # Should NOT raise (run_turn catches and produces a failed
    # AgentTurnResult). The buffer entry should reflect ok=False.
    result = await agent.run_turn("sess-err", "trigger")
    assert result.ok is False
    rows = agent.list_recently_finished()
    assert len(rows) == 1
    assert rows[0]["ok"] is False
