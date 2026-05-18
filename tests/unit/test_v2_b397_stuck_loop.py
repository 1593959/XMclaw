"""B-397 — anti-loop guard + apply_patch stale-match hint.

Real-world failure 2026-05-09: user edited
``C:\\Users\\15978\\Desktop\\xmclaw-architecture-redesign.md`` via
XMclaw. The LLM called ``apply_patch`` with old_text from a stale
view, got the legacy error string ``"file may have changed; re-read
it before patching"``, ignored it, and made the SAME stale-text edit
40 hops in a row until ``max_hops`` fired. Wasted ~$0.50 of LLM
budget per stuck turn.

This file pins both fixes:

1. ``apply_patch`` now embeds the CURRENT file content (or a
   fuzzy-match anchor with ±10 lines context) in the error string —
   the LLM gets the fresh state inline without needing another
   ``file_read`` round-trip.

2. ``agent_loop.run_turn`` tracks consecutive identical
   ``(tool_name, error_signature)`` pairs and breaks the hop loop
   after 3 in a row, surfacing a "stuck in a loop" error rather than
   running the budget to zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir.toolcall import ToolCall, ToolResult, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMResponse, Message
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.builtin import BuiltinTools


# ── apply_patch error message includes current file content ──


@pytest.mark.asyncio
async def test_b397_apply_patch_small_file_includes_full_current(
    tmp_path: Path,
) -> None:
    """When the file is small (≤4000 chars), the error embeds the
    entire current file so the LLM doesn't need to re-read."""
    f = tmp_path / "doc.md"
    f.write_text(
        "# Title\n\nlatest content the LLM hasn't seen yet\n",
        encoding="utf-8",
    )

    tools = BuiltinTools(allowed_dirs=[tmp_path])
    call = ToolCall(
        id="t1", name="apply_patch",
        args={
            "path": str(f),
            "edits": [{
                "old_text": "stale text the LLM is using",
                "new_text": "won't matter",
            }],
        },
        provenance="test",
    )
    result = await tools.invoke(call)
    assert not result.ok
    assert "old_text not found" in (result.error or "")
    # New: the actual current content is in the error.
    assert "latest content the LLM hasn't seen yet" in (result.error or "")
    # And the hint nudges the LLM not to re-read.
    assert "WITHOUT calling file_read" in (result.error or "")


@pytest.mark.asyncio
async def test_b397_apply_patch_partial_match_returns_anchor_context(
    tmp_path: Path,
) -> None:
    """When the file is large but a substring of old_text DOES match,
    the error returns ±10 lines around the anchor so the LLM can
    re-base."""
    # 5000+ chars to bypass the small-file path.
    body = "\n".join([f"line {i}: padding_padding_padding" for i in range(200)])
    body += "\n\nThe agent must remember to refactor THIS section.\n"
    body += "\n".join([f"after_line {i}: more_padding" for i in range(50)])
    f = tmp_path / "big.md"
    f.write_text(body, encoding="utf-8")

    tools = BuiltinTools(allowed_dirs=[tmp_path])
    call = ToolCall(
        id="t2", name="apply_patch",
        args={
            "path": str(f),
            "edits": [{
                # Stale: file says "must remember to refactor", LLM
                # has "needed to refactor" from an old view.
                "old_text": (
                    "The agent needed to refactor THIS section.\n"
                    "Old hallucinated continuation."
                ),
                "new_text": "...",
            }],
        },
        provenance="test",
    )
    result = await tools.invoke(call)
    assert not result.ok
    err = result.error or ""
    # Either the full-file path (if 4001-char threshold), or anchor
    # path. Both flavors include actionable context.
    assert (
        "CONTEXT" in err
        or "CURRENT FILE" in err
        or "HEAD" in err
    ), f"err missing context block: {err!r}"


@pytest.mark.asyncio
async def test_b397_apply_patch_no_match_returns_head_dump(
    tmp_path: Path,
) -> None:
    """When NOTHING of old_text matches, the error returns the file's
    first 80 lines."""
    # Each line is ~50 chars × 200 lines = 10000 chars > 4000-char
    # small-file threshold, forcing the no-match-head-dump path.
    body = "\n".join([f"actual line {i}: " + ("padding" * 5) for i in range(200)])
    f = tmp_path / "huge.md"
    f.write_text(body, encoding="utf-8")

    tools = BuiltinTools(allowed_dirs=[tmp_path])
    call = ToolCall(
        id="t3", name="apply_patch",
        args={
            "path": str(f),
            "edits": [{
                # Totally unrelated to anything in the file.
                "old_text": "z" * 300,
                "new_text": "y",
            }],
        },
        provenance="test",
    )
    result = await tools.invoke(call)
    assert not result.ok
    err = result.error or ""
    assert "actual line 0" in err
    assert "Re-anchor" in err


# ── agent_loop anti-loop guard ────────────────────────────────────


@dataclass
class _AlwaysFailToolProvider(ToolProvider):
    """Returns the same error every time — simulates a stale-text
    apply_patch loop where the LLM keeps making the same mistake."""

    error_msg: str = "edits[0].old_text not found in /tmp/foo.md — file may have changed"

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="apply_patch",
            description="patch a file",
            parameters_schema={"type": "object"},
        )]

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            ok=False,
            content=None,
            error=self.error_msg,
            latency_ms=0.0,
            side_effects=(),
        )


@dataclass
class _StuckLoopLLM:
    """LLM that ALWAYS calls apply_patch with the same args, regardless
    of what the tool returns. Models the failure pattern that triggered
    B-397: 40 hops in a row, all the same edit.
    """
    captured_messages: list[list[Message]] | None = None
    model: str = "stub"

    def __post_init__(self) -> None:
        self.captured_messages = []

    async def complete_streaming(
        self, messages: list[Message], *, tools: Any = None,
        on_chunk: Any = None, on_thinking_chunk: Any = None,
        on_tool_block: Any = None, cancel: Any = None,
    ) -> LLMResponse:
        assert self.captured_messages is not None
        self.captured_messages.append(list(messages))
        # Always call the same tool with the same args.
        return LLMResponse(
            content="",
            tool_calls=(
                ToolCall(
                    id=f"call-{len(self.captured_messages)}",
                    name="apply_patch",
                    args={
                        "path": "/tmp/foo.md",
                        "edits": [{"old_text": "stale", "new_text": "fresh"}],
                    },
                    provenance="synthetic",
                ),
            ),
        )

    async def complete(self, messages: list[Message], **_kw: Any) -> LLMResponse:
        return await self.complete_streaming(messages)


@pytest.mark.asyncio
async def test_b397_stuck_loop_breaks_after_three_consecutive() -> None:
    """3 consecutive identical (tool_name, error) pairs → loop breaks
    early with a 'stuck loop' message, BEFORE max_hops fires."""
    bus = InProcessEventBus()
    llm = _StuckLoopLLM()
    provider = _AlwaysFailToolProvider()
    loop = AgentLoop(
        llm=llm, bus=bus, tools=provider,
        max_hops=20,  # generous so we'd burn it without the guard
    )
    res = await loop.run_turn("sess-stuck", "fix the doc")
    assert res.ok is False
    assert "stuck in a loop" in (res.text or "").lower()
    assert "apply_patch" in (res.text or "")
    # Critically: hops should be ≤ 4 (3 stuck + the trigger), NOT 20.
    assert res.hops <= 4, (
        f"expected stuck-loop guard to break early at hop ≤ 4, got "
        f"hops={res.hops}; max_hops budget got burned anyway"
    )


@pytest.mark.asyncio
async def test_b397_different_errors_dont_trip_guard() -> None:
    """The deque resets on a different error_signature — qualitatively
    different errors don't count as 'stuck'."""
    bus = InProcessEventBus()
    error_sequence = [
        "edits[0].old_text not found",
        "permission denied",
        "edits[0].old_text not found",
        "file too large",
    ]

    @dataclass
    class _CycleFailProvider(ToolProvider):
        idx: int = 0

        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(
                name="apply_patch", description="patch",
                parameters_schema={"type": "object"},
            )]

        async def invoke(self, call: ToolCall) -> ToolResult:
            err = error_sequence[self.idx % len(error_sequence)]
            self.idx += 1
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=err, latency_ms=0.0, side_effects=(),
            )

    llm = _StuckLoopLLM()
    loop = AgentLoop(
        llm=llm, bus=bus, tools=_CycleFailProvider(),
        max_hops=4,
    )
    res = await loop.run_turn("sess-rotate", "do thing")
    # 4 hops × 1 different error each → never 3 in a row → guard
    # doesn't trip. We hit max_hops instead.
    assert "max_hops" in (res.error or "")


@pytest.mark.asyncio
async def test_b397_success_resets_streak() -> None:
    """One successful tool call resets the deque — even if errors
    accumulated before that, a recovery clears the streak."""
    bus = InProcessEventBus()
    sequence = [
        ("error", "edits[0].old_text not found"),
        ("error", "edits[0].old_text not found"),
        ("ok", "patched"),                          # reset
        ("error", "edits[0].old_text not found"),
        ("error", "edits[0].old_text not found"),
        # Need a 3rd identical error AFTER the success to trip.
        ("error", "edits[0].old_text not found"),
    ]

    @dataclass
    class _MixedProvider(ToolProvider):
        idx: int = 0

        def list_tools(self) -> list[ToolSpec]:
            return [ToolSpec(
                name="apply_patch", description="patch",
                parameters_schema={"type": "object"},
            )]

        async def invoke(self, call: ToolCall) -> ToolResult:
            kind, payload = sequence[self.idx]
            self.idx += 1
            if kind == "ok":
                return ToolResult(
                    call_id=call.id, ok=True, content=payload,
                    error=None, latency_ms=0.0, side_effects=(),
                )
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=payload, latency_ms=0.0, side_effects=(),
            )

    llm = _StuckLoopLLM()
    loop = AgentLoop(
        llm=llm, bus=bus, tools=_MixedProvider(),
        max_hops=10,
    )
    res = await loop.run_turn("sess-recover", "do thing")
    # First 2 errors → streak=2 (safe). Then success → reset. Then 3
    # consecutive errors → tripped at the 3rd.
    assert res.ok is False
    assert "stuck in a loop" in (res.text or "").lower()
    # hops: 2 errors + 1 success + 3 errors = 6 hops worth of LLM
    # calls. We expect to break on the 6th hop's tool result.
    assert res.hops <= 7


# ── meta-cognitive no-progress guard ──────────────────────────────────


@dataclass
class _AlwaysFailProvider(ToolProvider):
    """Every tool call fails with a DIFFERENT error — B-397 won't trip
    (because errors differ) but the no-progress guard will."""

    _count: int = 0

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="tool_a", description="a", parameters_schema={}),
            ToolSpec(name="tool_b", description="b", parameters_schema={}),
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        self._count += 1
        return ToolResult(
            call_id=call.id, ok=False, content=None,
            error=f"fail #{self._count}", latency_ms=0.0, side_effects=(),
        )


@pytest.mark.asyncio
async def test_no_progress_guard_trips_after_five_failed_hops() -> None:
    """When every tool call fails for 5 consecutive hops (different
    errors each time so B-397 does NOT fire), the no-progress guard
    should break the loop."""
    bus = InProcessEventBus()

    from xmclaw.providers.llm.base import LLMProvider

    @dataclass
    class _AlwaysToolLLM(LLMProvider):
        _i: int = 0
        model: str = "mock"

        async def stream(self, _messages, tools=None, *, cancel=None):
            if False:
                yield  # type: ignore[unreachable]

        async def complete(self, _messages, tools=None):
            self._i += 1
            # Emit a different tool each hop to avoid B-397.
            name = "tool_a" if self._i % 2 else "tool_b"
            return LLMResponse(
                content="",
                tool_calls=(ToolCall(
                    name=name, args={}, provenance="test", id=f"tc-{self._i}",
                ),),
            )

        @property
        def tool_call_shape(self):
            from xmclaw.core.ir.toolcall import ToolCallShape
            return ToolCallShape.ANTHROPIC_NATIVE

        @property
        def pricing(self):
            from xmclaw.providers.llm.base import Pricing
            return Pricing()

    llm = _AlwaysToolLLM()
    tools = _AlwaysFailProvider()
    loop = AgentLoop(llm=llm, bus=bus, tools=tools, max_hops=20)
    res = await loop.run_turn("sess-np", "do thing")

    assert res.ok is False
    assert "no progress" in (res.text or "").lower() or "no_progress" in (res.error or "")
    # Should break well before max_hops=20.
    assert res.hops <= 7
