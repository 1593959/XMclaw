"""B-332: FilteredToolProvider + AgentLoop.run_turn tools_allowlist.

Closes audit finding #7: ``CronJob.enabled_toolsets`` was declarative-
only — parsed, persisted, shown in the UI, but no path enforced it.
The wrapper here is the enforcement primitive; AgentLoop's new
``tools_allowlist`` kwarg is the plumbing; the cron runner in
daemon/app.py is the production caller.

Tests cover three layers:
  1. FilteredToolProvider unit behaviour (list_tools / invoke /
     edge cases — empty allowlist, unknown tool name)
  2. AgentLoop.run_turn(tools_allowlist=...) — disallowed tool
     call returns a structured failure that the agent can read
  3. The wrapper's allowed_names property (what the cron runner /
     audit logger reads)
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.filtered import FilteredToolProvider


class _DummyProvider(ToolProvider):
    """Minimal ToolProvider — N synthetic tools, async invoke records
    the call name + returns a fixed ok result."""

    def __init__(self, tool_names: list[str]) -> None:
        self._tools = [
            ToolSpec(name=n, description=f"dummy {n}", parameters_schema={})
            for n in tool_names
        ]
        self.invoked: list[str] = []

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def invoke(self, call: ToolCall) -> ToolResult:
        self.invoked.append(call.name)
        return ToolResult(call_id=call.id, ok=True, content=f"ran {call.name}")


# ── unit tests for FilteredToolProvider ────────────────────────────


def test_b332_list_tools_subsets_to_allowlist() -> None:
    inner = _DummyProvider(["bash", "file_read", "web_fetch", "memory_search"])
    wrapped = FilteredToolProvider(inner, allowed_names=["web_fetch", "memory_search"])
    names = sorted(t.name for t in wrapped.list_tools())
    assert names == ["memory_search", "web_fetch"]


def test_b332_list_tools_empty_allowlist_returns_nothing() -> None:
    """Explicit empty allowlist = "no tool allowed". Distinct from
    ``allowed_names=None`` which means "don't wrap at all" (the
    AgentLoop branch handles that — at FilteredToolProvider level,
    if you constructed it, you meant restriction)."""
    inner = _DummyProvider(["bash", "file_read"])
    wrapped = FilteredToolProvider(inner, allowed_names=[])
    assert wrapped.list_tools() == []


def test_b332_list_tools_unknown_name_silently_ignored() -> None:
    """An allowlist entry that doesn't match any inner tool just
    yields nothing — no crash. Common case: cron job declared
    ``enabled_toolsets=['web_fetch']`` but the daemon didn't wire web
    tools this run."""
    inner = _DummyProvider(["bash", "file_read"])
    wrapped = FilteredToolProvider(inner, allowed_names=["web_fetch"])
    assert wrapped.list_tools() == []


@pytest.mark.asyncio
async def test_b332_invoke_passes_through_allowed_tool() -> None:
    inner = _DummyProvider(["bash", "file_read"])
    wrapped = FilteredToolProvider(inner, allowed_names=["bash"])
    call = ToolCall(
        id="c1", name="bash", args={"command": "echo hi"},
        provenance="anthropic",
    )
    result = await wrapped.invoke(call)
    assert result.ok is True
    assert result.content == "ran bash"
    assert inner.invoked == ["bash"]


@pytest.mark.asyncio
async def test_b332_invoke_blocks_disallowed_tool() -> None:
    """Disallowed tool → structured ok=False with the disallowed
    name + the available allowlist in the error string. The agent
    loop sees this as a real refusal and can apologise / pick a
    different tool. Pre-B-332 the cron path silently allowed the
    call."""
    inner = _DummyProvider(["bash", "file_read", "web_fetch"])
    wrapped = FilteredToolProvider(inner, allowed_names=["web_fetch"])
    call = ToolCall(
        id="c1", name="bash", args={"command": "echo escape"},
        provenance="anthropic",
    )
    result = await wrapped.invoke(call)
    assert result.ok is False
    assert "bash" in (result.error or "")
    assert "allowlist" in (result.error or "").lower()
    # Inner provider was never reached.
    assert inner.invoked == []


@pytest.mark.asyncio
async def test_b332_empty_allowlist_blocks_everything() -> None:
    inner = _DummyProvider(["bash"])
    wrapped = FilteredToolProvider(inner, allowed_names=[])
    call = ToolCall(
        id="c1", name="bash", args={}, provenance="anthropic",
    )
    result = await wrapped.invoke(call)
    assert result.ok is False
    assert "no tools allowed" in (result.error or "").lower()


def test_b332_allowed_names_property_is_frozen() -> None:
    """Audit / log code reads ``allowed_names`` to surface what the
    cron job allowed. Frozen so callers can't mutate it after the
    wrapper is built (would defeat the per-turn isolation guarantee)."""
    inner = _DummyProvider(["a"])
    wrapped = FilteredToolProvider(inner, allowed_names=["a", "b", "a"])
    assert wrapped.allowed_names == frozenset({"a", "b"})


# ── AgentLoop integration ──────────────────────────────────────────


from dataclasses import dataclass, field  # noqa: E402

from xmclaw.core.ir import ToolCallShape  # noqa: E402
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Pricing  # noqa: E402


@dataclass
class _ScriptedLLM(LLMProvider):
    """Mirror of test_v2_agent_loop.py's scripted LLM. Returns the
    i-th response on the i-th call; satisfies the LLMProvider ABC
    including the ``tool_call_shape`` / ``pricing`` properties."""

    script: list[LLMResponse] = field(default_factory=list)
    model: str = "scripted"
    _i: int = 0

    async def stream(self, messages, tools=None, *, cancel=None):
        if False:
            yield  # type: ignore[unreachable]

    async def complete(self, messages, tools=None) -> LLMResponse:
        if self._i >= len(self.script):
            raise RuntimeError(
                f"_ScriptedLLM exhausted after {len(self.script)} calls"
            )
        resp = self.script[self._i]
        self._i += 1
        return resp

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.mark.asyncio
async def test_b332_run_turn_tools_allowlist_blocks_disallowed_call() -> None:
    """End-to-end: AgentLoop with a real tool stack, run_turn called
    with tools_allowlist={'allowed_tool'}. Scripted LLM emits a
    tool_call to ``forbidden_tool`` — wrapper rejects it; the inner
    provider's invoke counter stays at 0."""
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.core.ir import ToolCall as _TC
    from xmclaw.daemon.agent_loop import AgentLoop

    inner = _DummyProvider(["allowed_tool", "forbidden_tool"])
    bus = InProcessEventBus()

    # Hop 1: model calls forbidden_tool. Hop 2: model gives final answer.
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(_TC(
                id="tc-1", name="forbidden_tool",
                args={}, provenance="anthropic",
            ),),
        ),
        LLMResponse(content="couldn't use forbidden_tool", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=inner)

    res = await agent.run_turn(
        "sess-cron", "do something",
        tools_allowlist={"allowed_tool"},
    )

    # The forbidden tool was never invoked on the inner provider.
    assert inner.invoked == [], (
        f"forbidden_tool reached the inner provider; allowlist did "
        f"not enforce. invoked={inner.invoked!r}"
    )
    assert res.ok is True


@pytest.mark.asyncio
async def test_b332_run_turn_no_allowlist_unaffected() -> None:
    """Default chat path (no kwarg) — agent sees its full tool stack
    just as before. Regression guard: cron change didn't break user chat."""
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.core.ir import ToolCall as _TC
    from xmclaw.daemon.agent_loop import AgentLoop

    inner = _DummyProvider(["bash"])
    bus = InProcessEventBus()
    llm = _ScriptedLLM(script=[
        LLMResponse(
            content="",
            tool_calls=(_TC(
                id="tc-1", name="bash",
                args={"command": "echo"}, provenance="anthropic",
            ),),
        ),
        LLMResponse(content="done", tool_calls=()),
    ])
    agent = AgentLoop(llm=llm, bus=bus, tools=inner)

    res = await agent.run_turn("sess-chat", "hi")
    # Without allowlist, bash invoked normally.
    assert inner.invoked == ["bash"]
    assert res.ok is True
