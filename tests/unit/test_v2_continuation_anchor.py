"""B-186 — vague-continuation anchor.

Real-data finding from session ``chat-59bb7a7a`` on 2026-05-02:
the user asked the agent to self-audit; LLM provider hung at hop 6,
agent went silent. 10 minutes later the user typed ``继续``. The
agent picked up an unrelated MEMORY.md project entry and wandered
off-topic.

Fix: when (a) prior assistant turn never produced final synthesis
text AND (b) new user message is a vague continuation token like
``继续`` / ``continue``, prepend a routing hint that pins the LLM
to the in-flight topic instead of foraging persona context for new
work.

These tests pin the helper functions in ``daemon/agent_loop.py``.
"""
from __future__ import annotations

from xmclaw.daemon.agent_loop import (
    _continuation_anchor,
    _is_vague_continuation,
    _prior_ended_without_synthesis,
)
from xmclaw.providers.llm.base import Message


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


# ── _is_vague_continuation ────────────────────────────────────────


def test_is_vague_continuation_chinese_tokens() -> None:
    assert _is_vague_continuation("继续")
    assert _is_vague_continuation("接着")
    assert _is_vague_continuation("下一步")
    assert _is_vague_continuation(" 继续 ")  # whitespace tolerated


def test_is_vague_continuation_english_tokens() -> None:
    assert _is_vague_continuation("continue")
    assert _is_vague_continuation("go on")
    assert _is_vague_continuation("Keep Going")  # case-insensitive
    assert _is_vague_continuation("next")


def test_is_vague_continuation_rejects_real_messages() -> None:
    assert not _is_vague_continuation(
        "继续把那个 RAG 计划落地到 PR"
    )
    assert not _is_vague_continuation("continue the audit you started")
    assert not _is_vague_continuation("")
    assert not _is_vague_continuation("hello world how are you doing")


def test_is_vague_continuation_length_cap() -> None:
    """Anything longer than the curated tokens isn't a 'just keep
    going' signal — even if it starts with one."""
    assert not _is_vague_continuation("继续，但是先看一下我新写的这段")


# ── _prior_ended_without_synthesis ────────────────────────────────


def test_prior_ended_without_synthesis_empty_content() -> None:
    """Mirrors the real bug: the last assistant turn had tool calls
    but its text content was empty (LLM hung after hop 6)."""
    prior = [
        _msg("user", "check yourself"),
        _msg("assistant", ""),  # no synthesis
    ]
    assert _prior_ended_without_synthesis(prior)


def test_prior_ended_without_synthesis_with_trailing_tool_messages() -> None:
    """Tool messages between the assistant turn and the new user
    message must NOT mask the empty-synthesis signal."""
    prior = [
        _msg("user", "check yourself"),
        _msg("assistant", ""),  # the empty turn
        _msg("tool", "tool result A"),
        _msg("tool", "tool result B"),
    ]
    assert _prior_ended_without_synthesis(prior)


def test_prior_ended_with_synthesis_returns_false() -> None:
    """Normal completion: assistant produced text. No anchor needed."""
    prior = [
        _msg("user", "what's 2 plus 2?"),
        _msg("assistant", "It's 4."),
    ]
    assert not _prior_ended_without_synthesis(prior)


def test_prior_empty_history_returns_false() -> None:
    """First-turn case — no prior to anchor against."""
    assert not _prior_ended_without_synthesis([])


# ── _continuation_anchor (composition) ────────────────────────────


def test_anchor_fires_on_real_data_scenario() -> None:
    """The exact joint-audit scenario: empty prior synthesis +
    user types '继续'."""
    prior = [
        _msg("user", "检查一下你自身，是否存在路径问题"),
        _msg("assistant", ""),
        _msg("tool", "list_dir result"),
    ]
    anchor = _continuation_anchor(prior, "继续")
    assert "CONTINUE THAT INVESTIGATION" in anchor
    assert "MEMORY.md" in anchor
    assert "继续" in anchor


def test_anchor_does_not_fire_when_prior_finished_cleanly() -> None:
    """LLM produced a real reply last turn → '继续' likely means a
    new piece of work; don't pin to old context."""
    prior = [
        _msg("user", "audit"),
        _msg("assistant", "Here's the audit report: ..."),
    ]
    assert _continuation_anchor(prior, "继续") == ""


def test_anchor_does_not_fire_on_substantive_message() -> None:
    """Real new task even after a stalled turn — the LLM should
    follow the explicit instruction, not get pinned by an anchor."""
    prior = [
        _msg("user", "audit"),
        _msg("assistant", ""),
    ]
    out = _continuation_anchor(prior, "现在改改 MEMORY.md 让它更准确")
    assert out == ""


def test_anchor_first_turn_is_noop() -> None:
    """No prior history → no anchor. Avoids polluting the very
    first '继续' on a brand-new session."""
    assert _continuation_anchor([], "继续") == ""
