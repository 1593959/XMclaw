"""Unit tests for SubagentToolProvider (Batch C.1)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.builtin_subagent import SubagentToolProvider


# ── Stubs ────────────────────────────────────────────────────────


class _LLMResp:
    def __init__(self, content: str = "", tool_calls: tuple = ()) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _ScriptedLLM:
    """LLM stub with a programmable response queue.

    Each call pops the next entry. If queue is empty, returns the
    default. Tracks call count for assertions.
    """

    def __init__(
        self,
        responses: list[_LLMResp] | None = None,
        default: _LLMResp | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.queue: list[_LLMResp] = list(responses or [])
        self.default = default or _LLMResp(content="(default)")
        self.latency_s = latency_s
        self.calls = 0
        self.last_messages: list[Any] = []

    async def complete(self, messages, tools=None):
        self.calls += 1
        self.last_messages = list(messages)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.queue:
            return self.queue.pop(0)
        return self.default


class _StubTools(ToolProvider):
    """ToolProvider with a single ``echo`` tool returning whatever was
    passed in ``text``."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="echo",
                description="Return the text you were given.",
                parameters_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if call.name == "echo":
            return ToolResult(
                call_id=call.id, ok=True,
                content=str(call.args.get("text", "")),
                error=None,
            )
        return ToolResult(
            call_id=call.id, ok=False, content=None,
            error=f"unknown tool {call.name}",
        )


def _make_call(args: dict, name: str = "parallel_subagents") -> ToolCall:
    return ToolCall(
        id="t-fanout", name=name, args=args, provenance="synthetic",
    )


# ── Disable / passthrough cases ──────────────────────────────────


async def test_disabled_when_no_llm():
    wrap = SubagentToolProvider(llm=None, tools=None)
    r = await wrap.invoke(_make_call({"subtasks": ["a", "b"]}))
    assert r.ok is False
    assert "disabled" in r.error or "not wired" in r.error


async def test_disabled_kill_switch():
    wrap = SubagentToolProvider(llm=_ScriptedLLM(), tools=None, enabled=False)
    assert wrap.list_tools() == []
    r = await wrap.invoke(_make_call({"subtasks": ["a", "b"]}))
    assert r.ok is False


async def test_lists_tool_when_enabled():
    wrap = SubagentToolProvider(llm=_ScriptedLLM(), tools=None)
    specs = wrap.list_tools()
    assert len(specs) == 1
    assert specs[0].name == "parallel_subagents"


async def test_unknown_tool_call():
    wrap = SubagentToolProvider(llm=_ScriptedLLM(), tools=None)
    r = await wrap.invoke(_make_call({"subtasks": ["a", "b"]}, name="other"))
    assert r.ok is False
    assert "unknown tool" in r.error


# ── Validation ───────────────────────────────────────────────────


@pytest.mark.parametrize("bad_subtasks", [
    None, [], ["only one"], list("abcdefghi"),  # 1 or 9+ items
    ["valid", ""], ["valid", 123], "not a list",
])
async def test_rejects_invalid_subtasks(bad_subtasks):
    wrap = SubagentToolProvider(llm=_ScriptedLLM(), tools=None)
    r = await wrap.invoke(_make_call({"subtasks": bad_subtasks}))
    assert r.ok is False
    assert "2-8" in r.error or "non-empty" in r.error


# ── Pure-reasoning fanout ────────────────────────────────────────


async def test_fanout_pure_reasoning_concat():
    """Each subagent returns text immediately (no tool calls)."""
    llm = _ScriptedLLM(responses=[
        _LLMResp(content="leaf-A"),
        _LLMResp(content="leaf-B"),
        _LLMResp(content="leaf-C"),
    ])
    wrap = SubagentToolProvider(llm=llm, tools=None)
    r = await wrap.invoke(_make_call({"subtasks": ["task1", "task2", "task3"]}))
    assert r.ok is True
    summary = json.loads(r.content)
    assert summary["total"] == 3
    assert summary["completed"] == 3
    assert summary["failed"] == 0
    assert "leaf-A" in summary["result"]
    assert "leaf-B" in summary["result"]
    assert "leaf-C" in summary["result"]


async def test_fanout_runs_subagents_in_parallel():
    """3 subagents each sleeping 0.3s — should finish in well under 0.9s."""
    llm = _ScriptedLLM(
        default=_LLMResp(content="done"),
        latency_s=0.3,
    )
    wrap = SubagentToolProvider(llm=llm, tools=None, max_concurrency=8)
    import time
    t0 = time.perf_counter()
    r = await wrap.invoke(_make_call({"subtasks": ["a", "b", "c"]}))
    elapsed = time.perf_counter() - t0
    assert r.ok
    # Sequential would be 0.9s; parallel ≈ 0.3s. Generous bound.
    assert elapsed < 0.7, f"expected parallel exec, got {elapsed}s"


async def test_fanout_isolates_context():
    """Subagent A's messages don't reach subagent B's prompt."""
    llm = _ScriptedLLM(default=_LLMResp(content="done"))
    wrap = SubagentToolProvider(llm=llm, tools=None)
    await wrap.invoke(_make_call({"subtasks": ["FIRST", "SECOND"]}))
    # last_messages reflects the most recent subagent's view only.
    # Since asyncio.gather order is non-deterministic, just verify
    # the latest message buffer doesn't contain BOTH prompts.
    contents = [
        getattr(m, "content", "") for m in llm.last_messages
    ]
    joined = "\n".join(contents)
    assert ("FIRST" in joined) ^ ("SECOND" in joined), (
        f"context leaked across subagents: {joined!r}"
    )


# ── Failure handling ────────────────────────────────────────────


class _ExplodingLLM:
    """LLM that raises on the Nth call (0-indexed)."""

    def __init__(self, raise_on: int = 0) -> None:
        self.raise_on = raise_on
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls - 1 == self.raise_on:
            raise RuntimeError("boom")
        return _LLMResp(content=f"ok-{self.calls}")


async def test_one_subagent_failure_rolled_up():
    """One subagent throws — others' results still surface."""
    llm = _ExplodingLLM(raise_on=0)
    wrap = SubagentToolProvider(llm=llm, tools=None, max_concurrency=1)
    # Force sequential so the first call always explodes.
    r = await wrap.invoke(_make_call({"subtasks": ["A", "B"]}))
    assert r.ok is True  # fanout itself ok, partial results
    summary = json.loads(r.content)
    assert summary["failed"] == 1
    assert summary["completed"] == 1


async def test_fanout_timeout():
    llm = _ScriptedLLM(
        default=_LLMResp(content="done"),
        latency_s=2.0,
    )
    wrap = SubagentToolProvider(
        llm=llm, tools=None, fanout_timeout_s=10.0, per_subagent_timeout_s=5.0,
    )
    wrap._fanout_timeout = 0.3  # force a tight cap
    r = await wrap.invoke(_make_call({"subtasks": ["a", "b"]}))
    assert r.ok is False
    assert "fanout exceeded" in r.error


async def test_per_subagent_timeout():
    llm = _ScriptedLLM(
        default=_LLMResp(content="done"),
        latency_s=2.0,
    )
    wrap = SubagentToolProvider(
        llm=llm, tools=None, fanout_timeout_s=30.0,
        per_subagent_timeout_s=5.0,
    )
    wrap._per_timeout = 0.3
    r = await wrap.invoke(_make_call({"subtasks": ["a", "b"]}))
    assert r.ok is True  # fanout itself ok, both subagents timed out
    summary = json.loads(r.content)
    assert summary["failed"] == 2


# ── Tool-using subagent ──────────────────────────────────────────


async def test_subagent_invokes_inner_tool():
    """Subagent issues a tool call → inner tool runs → reasoning ends."""
    tool_call = ToolCall(
        id="tc-1", name="echo", args={"text": "hello"},
        provenance="synthetic",
    )
    llm = _ScriptedLLM(responses=[
        _LLMResp(content="thinking...", tool_calls=(tool_call,)),
        _LLMResp(content="final answer using tool"),
        _LLMResp(content="leaf-B"),
    ])
    tools = _StubTools()
    wrap = SubagentToolProvider(llm=llm, tools=tools, max_concurrency=1)
    r = await wrap.invoke(_make_call({"subtasks": ["task-A", "task-B"]}))
    assert r.ok is True
    summary = json.loads(r.content)
    assert summary["completed"] == 2
    # echo was called exactly once.
    assert len(tools.calls) == 1
    assert tools.calls[0].name == "echo"


async def test_nested_fanout_blocked():
    """Subagent can't recursively spawn more subagents."""
    nested = ToolCall(
        id="tc-nested", name="parallel_subagents",
        args={"subtasks": ["x", "y"]}, provenance="synthetic",
    )
    llm = _ScriptedLLM(responses=[
        _LLMResp(content="trying nested", tool_calls=(nested,)),
        _LLMResp(content="gave up nesting"),
    ])
    tools = _StubTools()
    wrap = SubagentToolProvider(llm=llm, tools=tools, max_concurrency=1)
    r = await wrap.invoke(_make_call({"subtasks": ["A", "B"]}))
    assert r.ok is True
    # No actual nested fanout happened — the only tool calls
    # should be empty (echo never called for the nested attempt).
    assert all(c.name != "parallel_subagents" for c in tools.calls)


async def test_subagent_hop_cap():
    """Subagent loops past max_hops_per_subagent without ever returning
    a text-only response → reported as failure."""
    looping_call = ToolCall(
        id="tc-loop", name="echo", args={"text": "loop"},
        provenance="synthetic",
    )
    # Every response has tool_calls — the loop never breaks naturally.
    llm = _ScriptedLLM(
        default=_LLMResp(content="more!", tool_calls=(looping_call,)),
    )
    tools = _StubTools()
    wrap = SubagentToolProvider(
        llm=llm, tools=tools, max_hops_per_subagent=3, max_concurrency=1,
    )
    r = await wrap.invoke(_make_call({"subtasks": ["A", "B"]}))
    assert r.ok is True
    summary = json.loads(r.content)
    assert summary["failed"] == 2
    for entry in summary["per_subagent"]:
        assert entry["ok"] is False
        assert "exhausted" in entry["error"]


# ── Late binding ─────────────────────────────────────────────────


async def test_late_binding():
    """build_tools_from_config constructs without LLM; set_llm + set_tools
    plumb dependencies later."""
    wrap = SubagentToolProvider(llm=None, tools=None)
    # Before binding → no LLM → disabled.
    r1 = await wrap.invoke(_make_call({"subtasks": ["A", "B"]}))
    assert r1.ok is False
    # After binding → works.
    wrap.set_llm(_ScriptedLLM(default=_LLMResp(content="bound")))
    r2 = await wrap.invoke(_make_call({"subtasks": ["A", "B"]}))
    assert r2.ok is True
