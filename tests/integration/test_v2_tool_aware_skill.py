"""ToolAwareReadAndSummarize — full LLM↔tool multi-turn handshake tests.

Uses a scripted MockLLM that returns tool_use on its first call and
plain text on its second. The second-turn text is the "summary". The
integration asserts the skill:
  * runs the tool,
  * feeds the result back to the LLM,
  * returns the final text as ``summary``,
  * records every tool invocation for the grader.

Plus two failure-mode tests:
  * LLM that never calls a tool — skill still produces a summary
    (the system prompt asks for one, but models sometimes don't),
  * LLM that loops tool_use forever — skill bounded by max_hops,
    returns ``ok=False``.
"""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.skills.base import SkillInput
from xmclaw.skills.demo.read_and_summarize import (
    DEMO_VARIANTS,
    ToolAwareReadAndSummarize,
)


@dataclass
class _ScriptedLLM(LLMProvider):
    """Returns the i-th scripted response on the i-th .complete() call."""

    script: list[LLMResponse] = field(default_factory=list)
    _i: int = 0

    async def stream(  # pragma: no cover — not used
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        *,
        cancel: asyncio.Event | None = None,  # noqa: ARG002
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],  # noqa: ARG002
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
    ) -> LLMResponse:
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


# ── happy path: model calls file_read then summarizes ─────────────────────


@pytest.mark.asyncio
async def test_tool_aware_two_turn_loop_produces_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "doc.txt"
        p.write_text(
            "Photosynthesis converts sunlight into glucose in chloroplasts.",
            encoding="utf-8",
        )
        tools = BuiltinTools(allowed_dirs=[tmp])

        # Turn 1: model returns a tool_use for file_read(p).
        # Turn 2: model returns the final summary as plain text.
        llm = _ScriptedLLM(script=[
            LLMResponse(
                content="",
                tool_calls=(ToolCall(
                    name="file_read",
                    args={"path": str(p)},
                    provenance="anthropic",
                    id="toolu_1",
                ),),
                prompt_tokens=100,
                completion_tokens=10,
            ),
            LLMResponse(
                content="Plants use sunlight in chloroplasts to make glucose.",
                tool_calls=(),
                prompt_tokens=150,
                completion_tokens=25,
            ),
        ])
        skill = ToolAwareReadAndSummarize(
            variant=DEMO_VARIANTS[0], llm=llm, tools=tools,
        )
        out = await skill.run(SkillInput(args={"file_path": str(p)}))
        assert out.ok is True
        assert out.result["summary"] == (
            "Plants use sunlight in chloroplasts to make glucose."
        )
        # Exactly one tool invocation happened (file_read).
        assert len(out.result["tool_calls"]) == 1
        inv = out.result["tool_calls"][0]
        assert inv["name"] == "file_read"
        assert inv["ok"] is True
        assert inv["side_effects"] == []  # read is pure


# ── no-tool fallback: model skips the tool and answers from thin air ──────


@pytest.mark.asyncio
async def test_llm_skips_tool_still_returns_summary() -> None:
    """Even though the system prompt asks for a tool call, some models
    will just answer directly. The skill should still return the text
    as the summary (downstream grader will likely score it low since
    the agent didn't actually read the file, but that's the grader's
    job — not the skill's)."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        llm = _ScriptedLLM(script=[
            LLMResponse(
                content="A short summary from memory.",
                tool_calls=(),
            ),
        ])
        skill = ToolAwareReadAndSummarize(
            variant=DEMO_VARIANTS[0], llm=llm, tools=tools,
        )
        out = await skill.run(SkillInput(args={"file_path": "/dev/null/x"}))
        assert out.ok is True
        assert out.result["summary"] == "A short summary from memory."
        assert out.result["tool_calls"] == []


# ── bounded hops: model keeps asking for tool calls forever ──────────────


@pytest.mark.asyncio
async def test_max_hops_halts_infinite_tool_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.txt"
        p.write_text("hi", encoding="utf-8")
        tools = BuiltinTools(allowed_dirs=[tmp])

        # Every response is another tool_use — the model never terminates.
        loop_response = LLMResponse(
            content="",
            tool_calls=(ToolCall(
                name="file_read",
                args={"path": str(p)},
                provenance="anthropic",
            ),),
        )
        llm = _ScriptedLLM(script=[loop_response] * 10)
        skill = ToolAwareReadAndSummarize(
            variant=DEMO_VARIANTS[0], llm=llm, tools=tools, max_hops=3,
        )
        out = await skill.run(SkillInput(args={"file_path": str(p)}))
        assert out.ok is False
        assert "max_hops" in out.result["error"]
        assert len(out.result["tool_calls"]) == 3


# ── missing required arg ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_file_path_returns_error() -> None:
    tools = BuiltinTools()
    llm = _ScriptedLLM()  # never called
    skill = ToolAwareReadAndSummarize(
        variant=DEMO_VARIANTS[0], llm=llm, tools=tools,
    )
    out = await skill.run(SkillInput(args={}))
    assert out.ok is False
    assert "file_path" in out.result["error"]


# ── tool failure is reported but doesn't crash the skill ─────────────────


@pytest.mark.asyncio
async def test_tool_error_surfaces_but_skill_continues() -> None:
    """file_read on a nonexistent path returns ok=False inside ToolResult;
    the skill appends that to tool_calls and hands back to the LLM. The
    model's next turn here decides to give up; we just check the skill
    records the invocation properly."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        missing = Path(tmp) / "does_not_exist.txt"

        llm = _ScriptedLLM(script=[
            LLMResponse(
                content="",
                tool_calls=(ToolCall(
                    name="file_read",
                    args={"path": str(missing)},
                    provenance="anthropic",
                ),),
            ),
            LLMResponse(
                content="I could not read the file, so there is nothing to summarize.",
                tool_calls=(),
            ),
        ])
        skill = ToolAwareReadAndSummarize(
            variant=DEMO_VARIANTS[0], llm=llm, tools=tools,
        )
        out = await skill.run(SkillInput(args={"file_path": str(missing)}))
        assert out.ok is True
        assert len(out.result["tool_calls"]) == 1
        assert out.result["tool_calls"][0]["ok"] is False
        assert "not found" in out.result["tool_calls"][0]["error"].lower()
