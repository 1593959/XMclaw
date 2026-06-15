"""#2 Checkpoint/rewind — history truncation + file rollback.

Drives AgentLoop's checkpoint API directly with a real UndoCabinet so
the test exercises the actual rewind path: roll back file mutations made
after a checkpoint AND truncate the conversation to that point.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pathlib import Path

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCallShape
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMChunk, LLMProvider, LLMResponse, Message, Pricing
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.security.undo_cabinet import UndoCabinet


@dataclass
class _StubLLM(LLMProvider):
    async def stream(self, m, tools=None, *, cancel=None) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # pragma: no cover
    async def complete(self, messages, tools=None):
        return LLMResponse(content="ok", tool_calls=())
    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE
    @property
    def pricing(self) -> Pricing:
        return Pricing()


def _agent(tmp: Path) -> tuple[AgentLoop, UndoCabinet]:
    bus = InProcessEventBus()
    cab = UndoCabinet(root=tmp / "undo")
    tools = BuiltinTools(allowed_dirs=[tmp], undo_cabinet=cab)
    agent = AgentLoop(llm=_StubLLM(), bus=bus, tools=tools)
    agent._undo_cabinet = cab  # type: ignore[attr-defined]
    return agent, cab


def test_create_and_list_checkpoints(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    agent._histories["s1"] = [Message(role="user", content="a")]
    cp = agent.create_checkpoint("s1", label="first")
    assert cp["history_len"] == 1
    assert agent.list_checkpoints("s1")[-1]["id"] == cp["id"]


@pytest.mark.asyncio
async def test_rewind_truncates_history(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    agent._histories["s1"] = [Message(role="user", content="turn1")]
    cp = agent.create_checkpoint("s1")  # history_len = 1
    agent._histories["s1"].extend([
        Message(role="assistant", content="r1"),
        Message(role="user", content="turn2"),
    ])
    res = await agent.rewind_to_checkpoint("s1", cp["id"])
    assert res["ok"] is True
    assert res["messages_removed"] == 2
    assert len(agent._histories["s1"]) == 1
    assert agent._histories["s1"][0].content == "turn1"


@pytest.mark.asyncio
async def test_rewind_rolls_back_file_mutation(tmp_path: Path) -> None:
    agent, cab = _agent(tmp_path)
    f = tmp_path / "code.py"
    f.write_text("v1\n", encoding="utf-8")
    cp = agent.create_checkpoint("s1")
    cab.record_file_mutation(path=f, action="file_write", session_id="s1")
    f.write_text("v2\n", encoding="utf-8")
    assert f.read_text(encoding="utf-8") == "v2\n"
    res = await agent.rewind_to_checkpoint("s1", cp["id"])
    assert res["ok"] is True
    assert res["files_restored_count"] == 1
    assert f.read_text(encoding="utf-8") == "v1\n"


@pytest.mark.asyncio
async def test_rewind_unknown_checkpoint(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    res = await agent.rewind_to_checkpoint("s1", "nope")
    assert res["ok"] is False


def test_checkpoint_rewind_http_endpoints(tmp_path: Path) -> None:
    """Front-back boundary: the real /checkpoints + /rewind HTTP routes."""
    from fastapi.testclient import TestClient
    from xmclaw.daemon.app import create_app

    bus = InProcessEventBus()
    cab = UndoCabinet(root=tmp_path / "undo")
    tools = BuiltinTools(allowed_dirs=[tmp_path], undo_cabinet=cab)
    agent = AgentLoop(llm=_StubLLM(), bus=bus, tools=tools)
    agent._undo_cabinet = cab  # type: ignore[attr-defined]
    agent._histories["s1"] = [Message(role="user", content="t1")]
    cp = agent.create_checkpoint("s1", label="cp1")
    agent._histories["s1"].append(Message(role="assistant", content="r1"))

    client = TestClient(create_app(bus=bus, agent=agent))
    r = client.get("/api/v2/sessions/s1/checkpoints")
    assert r.status_code == 200
    assert any(c["id"] == cp["id"] for c in r.json()["checkpoints"])

    r2 = client.post("/api/v2/sessions/s1/rewind", json={"checkpoint_id": cp["id"]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    assert len(agent._histories["s1"]) == 1

    r3 = client.post("/api/v2/sessions/s1/rewind", json={"checkpoint_id": "x"})
    assert r3.status_code == 404


@pytest.mark.asyncio
async def test_rewind_drops_later_checkpoints(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    agent._histories["s1"] = []
    cp1 = agent.create_checkpoint("s1", label="cp1")
    import time as _t
    _t.sleep(0.01)
    agent.create_checkpoint("s1", label="cp2")
    await agent.rewind_to_checkpoint("s1", cp1["id"])
    ids = [c["id"] for c in agent.list_checkpoints("s1")]
    assert cp1["id"] in ids
    assert len(ids) == 1
