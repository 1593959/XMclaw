"""P0-1: ContextCompressor unit tests.

Pin the 5-phase pipeline against drift. Tests are organised by phase:

  * Phase 1: prune (delegated to tool_result_prune; spot check only)
  * Phase 2-3: head/tail boundary alignment (orphan tool result, last
    user message anchoring, splitting tool_call/result groups)
  * Phase 4: summary generation (mock summarise_call)
  * Phase 5: assembly + tool-pair sanitisation
  * Public: should_compress / on_session_reset / anti-thrashing

The summarise_call is a fake async fn the test injects — no real LLM
hits the wire. ``estimate_messages_tokens_rough`` is also tested as
the threshold mechanism depends on it.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from xmclaw.context.compressor import (
    ContextCompressor,
    SUMMARY_PREFIX,
    estimate_messages_tokens_rough,
)
from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.base import Message


# ── Helpers ──────────────────────────────────────────────────────────


def _msg(role: str, content: str = "", **kw) -> Message:
    return Message(role=role, content=content, **kw)


def _tc(name: str, args: dict | None = None, id: str = "") -> ToolCall:
    kwargs = {"name": name, "args": args or {}, "provenance": "synthetic"}
    if id:
        kwargs["id"] = id
    return ToolCall(**kwargs)


async def _stub_summarize(prompt: str, max_tokens: int) -> Optional[str]:
    """Default test summariser — returns a marker so we can verify
    the compressor wired through correctly."""
    return f"FAKE_SUMMARY len={len(prompt)} budget={max_tokens}"


async def _failing_summarize(prompt: str, max_tokens: int) -> Optional[str]:
    """Simulates a summariser that crashes (cooldown path)."""
    raise RuntimeError("summariser unavailable")


def _build_long_history(n_user_turns: int = 10, content_chars: int = 4000) -> list[Message]:
    """Build a synthetic long history.

    Pattern: system + (user + assistant + tool) × N. Each turn has
    ``content_chars`` characters in the user message + assistant
    text + tool result, so total tokens ≈ n_user_turns *
    (content_chars*3 / 4 + 30).
    """
    msgs: list[Message] = [_msg("system", "You are XMclaw.")]
    for i in range(n_user_turns):
        msgs.append(_msg("user", f"Q{i}: " + ("x" * content_chars)))
        msgs.append(_msg(
            "assistant", f"A{i}: " + ("y" * content_chars),
            tool_calls=(_tc("bash", {"command": "ls"}, id=f"call_{i}"),),
        ))
        msgs.append(_msg(
            "tool",
            content=f"OUT-{i}: " + ("z" * content_chars),
            tool_call_id=f"call_{i}",
        ))
    return msgs


# ── estimate_messages_tokens_rough ──────────────────────────────────


def test_estimate_empty() -> None:
    assert estimate_messages_tokens_rough([]) == 0


def test_estimate_text_only() -> None:
    msgs = [_msg("user", "x" * 400)]  # ~100 tokens (chars/4) + 10 overhead
    assert 100 <= estimate_messages_tokens_rough(msgs) <= 120


def test_estimate_includes_tool_args() -> None:
    msgs = [_msg(
        "assistant", "",
        tool_calls=(_tc("file_write", {"content": "a" * 800}),),
    )]
    # 800 / 4 = 200 from args + 10 overhead (no content)
    est = estimate_messages_tokens_rough(msgs)
    assert 200 <= est <= 220


# ── should_compress / anti-thrashing ────────────────────────────────


def test_should_compress_threshold() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        context_length=100_000, threshold_percent=0.5, quiet_mode=True,
    )
    # threshold = 50_000
    assert cc.should_compress(40_000) is False
    assert cc.should_compress(60_000) is True


def test_should_compress_anti_thrashing() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        context_length=100_000, quiet_mode=True,
    )
    cc._state("s1").ineffective_count = 2
    assert cc.should_compress(80_000, session_id="s1") is False
    # Different session should not be affected
    assert cc.should_compress(80_000, session_id="s2") is True


def test_on_session_reset_clears_state() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        context_length=100_000, quiet_mode=True,
    )
    cc._state("s1").ineffective_count = 5
    cc._state("s1").previous_summary = "X"
    cc.on_session_reset("s1")
    assert "s1" not in cc._states


# ── Compress: skip when too small ───────────────────────────────────


def test_compress_skips_short_history() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        protect_first_n=3, quiet_mode=True,
    )
    msgs = [_msg("system", "S"), _msg("user", "u1"), _msg("assistant", "a1")]
    out = asyncio.run(cc.compress(msgs))
    assert out == msgs  # untouched


# ── Compress: full pipeline happy path ──────────────────────────────


def test_compress_inserts_summary_message() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        protect_first_n=2, protect_last_n=4,
        context_length=20_000, threshold_percent=0.5,
        quiet_mode=True,
    )
    msgs = _build_long_history(n_user_turns=8, content_chars=600)
    out = asyncio.run(cc.compress(msgs, session_id="s"))
    # Compressed should be shorter
    assert len(out) < len(msgs)
    # SUMMARY_PREFIX appears in at least one message
    found = any(SUMMARY_PREFIX in (m.content or "") for m in out)
    assert found, "SUMMARY_PREFIX not found in compressed messages"


def test_compress_preserves_first_user_message() -> None:
    """Head protection: protect_first_n messages NEVER get summarised."""
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        protect_first_n=2, protect_last_n=4,
        context_length=20_000, threshold_percent=0.5, quiet_mode=True,
    )
    msgs = _build_long_history(n_user_turns=8, content_chars=600)
    head = msgs[:2]
    out = asyncio.run(cc.compress(msgs, session_id="s"))
    assert out[0].role == "system"  # system prompt always at index 0
    # The user message originally at index 1 should still be at index 1
    # (with possibly a system-prompt compaction note appended).
    assert out[1].role == head[1].role
    assert out[1].content == head[1].content


def test_compress_preserves_last_user_message() -> None:
    """The most recent user message must survive (#10896 fix).

    Without the anchor, ``_align_boundary_backward`` could pull the
    cut past a user message and the active task would silently
    disappear into the summary."""
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize,
        protect_first_n=2, protect_last_n=2,
        context_length=20_000, threshold_percent=0.5, quiet_mode=True,
    )
    msgs = _build_long_history(n_user_turns=8, content_chars=600)
    last_user = msgs[-3]  # last user message in our pattern
    assert last_user.role == "user"
    out = asyncio.run(cc.compress(msgs, session_id="s"))
    # Last user message must appear in the output
    found = any(
        m.role == "user" and m.content == last_user.content for m in out
    )
    assert found, "Last user message lost during compression"


def test_compress_failed_summary_uses_fallback() -> None:
    """When summarise_call raises, a static fallback notice is inserted."""
    cc = ContextCompressor(
        model="t", summarize_call=_failing_summarize,
        protect_first_n=2, protect_last_n=2,
        context_length=20_000, threshold_percent=0.5, quiet_mode=True,
    )
    msgs = _build_long_history(n_user_turns=8, content_chars=600)
    out = asyncio.run(cc.compress(msgs, session_id="s"))
    found_fallback = any(
        "Summary generation was unavailable" in (m.content or "") for m in out
    )
    assert found_fallback


def test_compress_returns_input_on_internal_error() -> None:
    """Catastrophic exception inside compress → return original messages.
    Context compression NEVER fails a user turn."""

    async def _bad_summarize(p: str, t: int) -> Optional[str]:
        return "ok"  # not the failure point

    cc = ContextCompressor(
        model="t", summarize_call=_bad_summarize,
        protect_first_n=2, quiet_mode=True,
    )
    # Simulate a path that crashes
    cc._find_tail_cut_by_tokens = lambda *a, **k: 1 / 0  # type: ignore
    msgs = _build_long_history(n_user_turns=6)
    out = asyncio.run(cc.compress(msgs, session_id="s"))
    assert out == msgs  # untouched


# ── Tool-pair sanitisation ──────────────────────────────────────────


def test_sanitize_drops_orphan_tool_results() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize, quiet_mode=True,
    )
    # tool result references "call_X" but no assistant message has it
    msgs = [
        _msg("system", "S"),
        _msg("user", "Q"),
        _msg("assistant", "A", tool_calls=(_tc("bash", {"c": "ls"}, id="call_1"),)),
        _msg("tool", "OUT-1", tool_call_id="call_1"),
        _msg("tool", "OUT-X", tool_call_id="call_X"),  # orphan
    ]
    sanitized = cc._sanitize_tool_pairs(msgs)
    assert len(sanitized) == len(msgs) - 1
    assert all(
        not (m.role == "tool" and m.tool_call_id == "call_X")
        for m in sanitized
    )


def test_sanitize_adds_stub_for_missing_results() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize, quiet_mode=True,
    )
    # assistant has tool_call but no matching tool result
    msgs = [
        _msg("system", "S"),
        _msg("user", "Q"),
        _msg("assistant", "A", tool_calls=(_tc("bash", {}, id="call_42"),)),
    ]
    sanitized = cc._sanitize_tool_pairs(msgs)
    assert len(sanitized) == len(msgs) + 1
    last = sanitized[-1]
    assert last.role == "tool"
    assert last.tool_call_id == "call_42"
    assert "earlier conversation" in (last.content or "").lower()


# ── Iterative summary (per-session state) ───────────────────────────


def test_iterative_summary_uses_previous() -> None:
    """Second compaction passes ``previous_summary`` to the summariser."""
    seen_prompts: list[str] = []

    async def _capture(prompt: str, max_tokens: int) -> Optional[str]:
        seen_prompts.append(prompt)
        return "NEW_SUMMARY"

    cc = ContextCompressor(
        model="t", summarize_call=_capture,
        protect_first_n=2, protect_last_n=2,
        context_length=20_000, threshold_percent=0.5, quiet_mode=True,
    )
    msgs = _build_long_history(n_user_turns=8, content_chars=600)
    asyncio.run(cc.compress(msgs, session_id="s"))
    asyncio.run(cc.compress(msgs, session_id="s"))
    # Second prompt should reference the previous summary
    assert len(seen_prompts) == 2
    assert "PREVIOUS SUMMARY" in seen_prompts[1]
    assert "NEW_SUMMARY" in seen_prompts[1]


# ── Boundary alignment ──────────────────────────────────────────────


def test_align_boundary_forward_skips_orphan_tool_results() -> None:
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize, quiet_mode=True,
    )
    msgs = [
        _msg("system", "S"),
        _msg("tool", "orphan", tool_call_id="x"),
        _msg("tool", "orphan2", tool_call_id="y"),
        _msg("user", "Q"),
    ]
    assert cc._align_boundary_forward(msgs, 1) == 3


def test_align_boundary_backward_pulls_back_to_assistant() -> None:
    """Boundary inside or right after a tool group pulls back to the
    parent assistant so the whole group gets summarised together
    (not split)."""
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize, quiet_mode=True,
    )
    msgs = [
        _msg("user", "Q"),
        _msg("assistant", "A", tool_calls=(_tc("bash", {}, id="c1"),)),
        _msg("tool", "OUT", tool_call_id="c1"),
        _msg("user", "Q2"),
    ]
    # Cut at index 3: immediately preceded by a tool result whose
    # parent assistant is at idx=1 — pull back so the whole group
    # gets summarised together.
    assert cc._align_boundary_backward(msgs, 3) == 1
    # Cut at index 2 (mid tool group) — pull back to 1 (assistant)
    assert cc._align_boundary_backward(msgs, 2) == 1


def test_align_boundary_backward_no_pullback_when_no_tool_group() -> None:
    """If there's no tool group immediately before the cut, no change."""
    cc = ContextCompressor(
        model="t", summarize_call=_stub_summarize, quiet_mode=True,
    )
    msgs = [
        _msg("user", "Q"),
        _msg("assistant", "A"),  # no tool_calls
        _msg("user", "Q2"),
        _msg("assistant", "A2"),
    ]
    assert cc._align_boundary_backward(msgs, 3) == 3
