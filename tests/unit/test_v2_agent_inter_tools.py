"""AgentInterTools — Epic #17 Phase 5 + 6.

Stubs the manager + AgentLoop surfaces so this test doesn't spin up a
daemon. What we verify:

* ``list_agents`` emits the primary synthetically + every manager id,
  carries ready flags, de-dupes if the manager somehow holds the
  primary id.
* ``chat_with_agent`` routes ``"main"`` → primary and other ids →
  manager; awaits ``run_turn``; returns the last assistant message.
* ``submit_to_agent`` returns immediately with a ``task_id``; the
  background coroutine populates the record; ``check_agent_task``
  reads status transitions pending → running → done (or error when
  the callee raises).
* Validation: missing / wrong-type args surface as ``ok=False`` with
  a human-readable error, not a traceback.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.agent_context import use_current_agent_id
from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.agent_inter import (
    AgentInterTools,
    _TaskRecord,
    _extract_last_assistant,
    _make_a2a_session_id,
    _prepend_caller_marker,
)


# ── stubs ────────────────────────────────────────────────────────────────


@dataclass
class _StubMessage:
    role: str
    content: str


@dataclass
class _StubLoop:
    """Fake AgentLoop that records run_turn invocations + seeds a reply.

    ``reply_template`` is formatted with the incoming content so a test
    can assert the reply was derived from the prompt rather than
    hard-coded. ``turns_seen`` lets the test inspect how many runs
    happened and in what order.
    """

    reply_template: str = "echo: {content}"
    raise_on_turn: Exception | None = None
    _histories: dict[str, list[_StubMessage]] = field(default_factory=dict)
    turns_seen: list[tuple[str, str]] = field(default_factory=list)

    async def run_turn(self, session_id: str, content: str) -> None:
        self.turns_seen.append((session_id, content))
        if self.raise_on_turn is not None:
            raise self.raise_on_turn
        reply = self.reply_template.format(content=content)
        self._histories.setdefault(session_id, []).extend([
            _StubMessage(role="user", content=content),
            _StubMessage(role="assistant", content=reply),
        ])


@dataclass
class _StubWorkspace:
    agent_id: str
    agent_loop: _StubLoop | None
    _ready: bool = True

    def is_ready(self) -> bool:
        return self._ready


class _StubManager:
    def __init__(self, workspaces: dict[str, _StubWorkspace] | None = None) -> None:
        self._ws: dict[str, _StubWorkspace] = workspaces or {}

    def list_ids(self) -> list[str]:
        return list(self._ws.keys())

    def get(self, agent_id: str):
        return self._ws.get(agent_id)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._ws

    def add(self, ws: _StubWorkspace) -> None:
        self._ws[ws.agent_id] = ws


def _call(name: str, **args: Any) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


# ── list_tools ───────────────────────────────────────────────────────────


def test_list_tools_advertises_all_four() -> None:
    mgr = _StubManager()
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    names = {s.name for s in tools.list_tools()}
    assert names == {
        "list_agents", "chat_with_agent",
        "submit_to_agent", "check_agent_task",
    }


# ── list_agents ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_agents_with_primary_and_workers() -> None:
    primary = _StubLoop()
    mgr = _StubManager({
        "alpha": _StubWorkspace("alpha", _StubLoop()),
        "beta": _StubWorkspace("beta", _StubLoop(), _ready=False),
    })
    tools = AgentInterTools(manager=mgr, primary_loop=primary)
    result = await tools.invoke(_call("list_agents"))
    assert result.ok
    body = json.loads(result.content)
    assert body["agents"][0] == {"agent_id": "main", "ready": True, "primary": True}
    workers = {row["agent_id"]: row for row in body["agents"][1:]}
    assert workers["alpha"]["ready"] is True and workers["alpha"]["primary"] is False
    assert workers["beta"]["ready"] is False


@pytest.mark.asyncio
async def test_list_agents_without_primary() -> None:
    # No primary_loop — e.g., echo-mode daemon. list_agents still works
    # and just omits the synthetic "main" row.
    mgr = _StubManager({"solo": _StubWorkspace("solo", _StubLoop())})
    tools = AgentInterTools(manager=mgr, primary_loop=None)
    result = await tools.invoke(_call("list_agents"))
    body = json.loads(result.content)
    assert [row["agent_id"] for row in body["agents"]] == ["solo"]


@pytest.mark.asyncio
async def test_list_agents_skips_duplicate_primary_id() -> None:
    # Defensive: if the manager somehow holds "main", don't double-emit.
    mgr = _StubManager({"main": _StubWorkspace("main", _StubLoop())})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    result = await tools.invoke(_call("list_agents"))
    body = json.loads(result.content)
    assert [row["agent_id"] for row in body["agents"]] == ["main"]
    assert body["agents"][0]["primary"] is True


# ── chat_with_agent ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_with_agent_routes_main_to_primary() -> None:
    primary = _StubLoop()
    worker = _StubLoop()
    mgr = _StubManager({"worker": _StubWorkspace("worker", worker)})
    tools = AgentInterTools(manager=mgr, primary_loop=primary)
    result = await tools.invoke(
        _call("chat_with_agent", agent_id="main", content="hi primary"),
    )
    assert result.ok
    # Phase 6 stamps the outgoing content with [Agent <caller> requesting].
    # Default caller (no ambient id) → primary_id == "main".
    assert result.content == "echo: [Agent main requesting]\n\nhi primary"
    # worker not called.
    assert worker.turns_seen == []
    assert len(primary.turns_seen) == 1
    assert primary.turns_seen[0][1] == "[Agent main requesting]\n\nhi primary"


@pytest.mark.asyncio
async def test_chat_with_agent_routes_to_worker() -> None:
    primary = _StubLoop()
    worker = _StubLoop(reply_template="worker saw: {content}")
    mgr = _StubManager({"worker": _StubWorkspace("worker", worker)})
    tools = AgentInterTools(manager=mgr, primary_loop=primary)
    result = await tools.invoke(
        _call("chat_with_agent", agent_id="worker", content="delegate"),
    )
    assert result.content == "worker saw: [Agent main requesting]\n\ndelegate"
    assert primary.turns_seen == []
    assert worker.turns_seen[0][1] == "[Agent main requesting]\n\ndelegate"


@pytest.mark.asyncio
async def test_chat_with_agent_unknown_id_errors() -> None:
    tools = AgentInterTools(manager=_StubManager(), primary_loop=_StubLoop())
    result = await tools.invoke(
        _call("chat_with_agent", agent_id="nope", content="x"),
    )
    assert not result.ok
    assert "unknown agent_id" in (result.error or "")


@pytest.mark.asyncio
async def test_chat_with_agent_missing_args_errors() -> None:
    tools = AgentInterTools(manager=_StubManager(), primary_loop=_StubLoop())
    r1 = await tools.invoke(_call("chat_with_agent", content="x"))
    assert not r1.ok and "agent_id required" in (r1.error or "")
    r2 = await tools.invoke(_call("chat_with_agent", agent_id="main"))
    assert not r2.ok and "content required" in (r2.error or "")


@pytest.mark.asyncio
async def test_chat_with_agent_not_ready_errors() -> None:
    # agent_loop is None → tool surfaces a clean error (not AttributeError).
    mgr = _StubManager({"half": _StubWorkspace("half", None)})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    result = await tools.invoke(
        _call("chat_with_agent", agent_id="half", content="x"),
    )
    assert not result.ok
    assert "not ready" in (result.error or "")


# ── submit_to_agent + check_agent_task ───────────────────────────────────


@pytest.mark.asyncio
async def test_submit_and_check_done_path() -> None:
    worker = _StubLoop(reply_template="done: {content}")
    mgr = _StubManager({"worker": _StubWorkspace("worker", worker)})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())

    submit = await tools.invoke(
        _call("submit_to_agent", agent_id="worker", content="bg job"),
    )
    assert submit.ok
    task_id = json.loads(submit.content)["task_id"]
    assert isinstance(task_id, str) and task_id

    # Yield so the background asyncio.create_task can run the turn.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    check = await tools.invoke(_call("check_agent_task", task_id=task_id))
    body = json.loads(check.content)
    assert body["status"] == "done"
    assert body["reply"] == "done: [Agent main requesting]\n\nbg job"
    assert body["agent_id"] == "worker"


@pytest.mark.asyncio
async def test_submit_to_unknown_agent_errors_immediately() -> None:
    # Resolution is synchronous — the tool fails fast instead of
    # scheduling a doomed background task.
    tools = AgentInterTools(manager=_StubManager(), primary_loop=_StubLoop())
    result = await tools.invoke(
        _call("submit_to_agent", agent_id="ghost", content="x"),
    )
    assert not result.ok
    assert "unknown agent_id" in (result.error or "")


@pytest.mark.asyncio
async def test_check_unknown_task_errors() -> None:
    tools = AgentInterTools(manager=_StubManager(), primary_loop=_StubLoop())
    result = await tools.invoke(_call("check_agent_task", task_id="nope"))
    assert not result.ok
    assert "unknown task_id" in (result.error or "")


@pytest.mark.asyncio
async def test_submit_captures_background_exception() -> None:
    # run_turn raising must land in the task record, not propagate out
    # of the daemon.
    angry = _StubLoop(raise_on_turn=RuntimeError("kaboom"))
    mgr = _StubManager({"angry": _StubWorkspace("angry", angry)})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    submit = await tools.invoke(
        _call("submit_to_agent", agent_id="angry", content="x"),
    )
    task_id = json.loads(submit.content)["task_id"]
    for _ in range(5):
        await asyncio.sleep(0)
    check = await tools.invoke(_call("check_agent_task", task_id=task_id))
    body = json.loads(check.content)
    assert body["status"] == "error"
    assert "RuntimeError" in body["error"]
    assert "kaboom" in body["error"]


@pytest.mark.asyncio
async def test_task_bookkeeping_cap() -> None:
    # Simulate the _MAX_TASKS drop-oldest path without actually filling
    # 256 slots — poke the internal cap down via a direct tasks dict.
    from xmclaw.providers.tool import agent_inter as mod
    mgr = _StubManager({"w": _StubWorkspace("w", _StubLoop())})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    old = mod._MAX_TASKS
    mod._MAX_TASKS = 2
    try:
        t1 = _TaskRecord(task_id="a", agent_id="w", session_id="s1", content="1")
        t2 = _TaskRecord(task_id="b", agent_id="w", session_id="s2", content="2")
        t3 = _TaskRecord(task_id="c", agent_id="w", session_id="s3", content="3")
        tools._store_task(t1)
        tools._store_task(t2)
        tools._store_task(t3)
        # Oldest ("a") evicted.
        assert list(tools._tasks.keys()) == ["b", "c"]
    finally:
        mod._MAX_TASKS = old


# ── unknown tool name ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_unknown_tool_is_clean_error() -> None:
    tools = AgentInterTools(manager=_StubManager(), primary_loop=_StubLoop())
    result = await tools.invoke(_call("not_a_thing"))
    assert not result.ok
    assert "unknown tool" in (result.error or "")


# ── helper units ─────────────────────────────────────────────────────────


def test_make_a2a_session_id_shape() -> None:
    sid = _make_a2a_session_id(caller="main", callee="worker-1")
    assert sid.startswith("main:to:worker-1:")
    parts = sid.split(":")
    # Phase 6 format: {caller}:to:{callee}:{ts}:{uuid8}
    assert len(parts) == 5
    assert parts[0] == "main"
    assert parts[1] == "to"
    assert parts[2] == "worker-1"
    assert parts[3].isdigit()            # ms timestamp
    assert parts[4].isalnum() and len(parts[4]) == 8


def test_extract_last_assistant_prefers_latest() -> None:
    loop = _StubLoop()
    loop._histories["s"] = [
        _StubMessage(role="assistant", content="first"),
        _StubMessage(role="user", content="hm"),
        _StubMessage(role="assistant", content="second"),
    ]
    assert _extract_last_assistant(loop, "s") == "second"


def test_extract_last_assistant_empty_history() -> None:
    # Empty / missing session returns empty string, not None — the
    # caller stuffs this into JSON and the LLM reads it, so None would
    # surface as the literal string "null".
    assert _extract_last_assistant(_StubLoop(), "missing") == ""


# ── Phase 6: caller resolution + prefix ─────────────────────────────────


def test_prepend_caller_marker_idempotent() -> None:
    # First stamp adds the prefix.
    stamped = _prepend_caller_marker("hello", "main")
    assert stamped == "[Agent main requesting]\n\nhello"
    # Re-stamp leaves it alone — nested delegation would otherwise
    # accumulate [Agent ...] banners on every hop.
    twice = _prepend_caller_marker(stamped, "worker")
    assert twice == stamped


@pytest.mark.asyncio
async def test_chat_uses_ambient_caller_id() -> None:
    # The contextvar set by the WS handler (Phase 4) determines who the
    # callee sees as the requester — not ``primary_id``.
    worker = _StubLoop(reply_template="worker saw: {content}")
    mgr = _StubManager({"worker": _StubWorkspace("worker", worker)})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    with use_current_agent_id("qa"):
        result = await tools.invoke(
            _call("chat_with_agent", agent_id="worker", content="review pls"),
        )
    assert result.ok
    assert worker.turns_seen[0][1] == "[Agent qa requesting]\n\nreview pls"
    # And the session id carries the same caller → callee edge.
    sid = worker.turns_seen[0][0]
    assert sid.startswith("qa:to:worker:")


@pytest.mark.asyncio
async def test_submit_uses_ambient_caller_id_in_session_and_content() -> None:
    worker = _StubLoop(reply_template="bg: {content}")
    mgr = _StubManager({"worker": _StubWorkspace("worker", worker)})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop())
    with use_current_agent_id("planner"):
        submit = await tools.invoke(
            _call("submit_to_agent", agent_id="worker", content="do thing"),
        )
    task_id = json.loads(submit.content)["task_id"]
    # Drain the background task.
    for _ in range(3):
        await asyncio.sleep(0)
    check = await tools.invoke(_call("check_agent_task", task_id=task_id))
    body = json.loads(check.content)
    assert body["status"] == "done"
    assert body["reply"] == "bg: [Agent planner requesting]\n\ndo thing"
    sid = worker.turns_seen[0][0]
    assert sid.startswith("planner:to:worker:")


@pytest.mark.asyncio
async def test_caller_defaults_to_primary_id_without_contextvar() -> None:
    # Outside a scoped turn the ambient id is None — falls back to the
    # configured primary so CLI / test calls still produce well-formed
    # session ids.
    worker = _StubLoop()
    mgr = _StubManager({"worker": _StubWorkspace("worker", worker)})
    tools = AgentInterTools(manager=mgr, primary_loop=_StubLoop(),
                            primary_id="root")
    await tools.invoke(
        _call("chat_with_agent", agent_id="worker", content="x"),
    )
    sid = worker.turns_seen[0][0]
    assert sid.startswith("root:to:worker:")
    assert worker.turns_seen[0][1].startswith("[Agent root requesting]")
