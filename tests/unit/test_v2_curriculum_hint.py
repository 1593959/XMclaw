"""B-202 — passive trigger for ``propose_curriculum_edit``.

Probe round B (probe_b200_v2) observed the agent identifying the
perfect curriculum-edit case (self_review_recent scenario) but
never firing the tool. Dormant evolution tools fade from the LLM's
working set without a contextual cue.

Fix: detect frustration / pushback markers in the current user
message and, once per session, append a ``<curriculum-hint>`` block
to the user message reminding the agent the tool exists and what the
two-step response looks like.

These tests pin the helper + the sanitiser extension in
``daemon/agent_loop.py``.
"""
from __future__ import annotations

from xmclaw.daemon.agent_loop import (
    _detect_frustration_signal,
    _sanitize_memory_context,
)


# ── _detect_frustration_signal ────────────────────────────────────


def test_frustration_chinese_markers_fire() -> None:
    assert _detect_frustration_signal("为什么你又这样")
    assert _detect_frustration_signal("我之前说过别这样做")
    assert _detect_frustration_signal("你看看，错了")
    assert _detect_frustration_signal("你不要再问我了")
    assert _detect_frustration_signal("不是这样的")
    assert _detect_frustration_signal("太离谱了")


def test_frustration_english_markers_fire() -> None:
    assert _detect_frustration_signal("Why are you doing that?")
    assert _detect_frustration_signal("That's wrong")
    assert _detect_frustration_signal("I told you already")
    assert _detect_frustration_signal("You keep ignoring me")
    assert _detect_frustration_signal("you should not refuse")
    assert _detect_frustration_signal("I didn't ask for that")


def test_frustration_case_insensitive_english() -> None:
    """Markers must match regardless of the user's caps."""
    assert _detect_frustration_signal("WHY ARE YOU IGNORING ME")
    assert _detect_frustration_signal("That's NOT what I asked")


def test_frustration_no_signal_on_neutral_messages() -> None:
    assert not _detect_frustration_signal("Please write a haiku.")
    assert not _detect_frustration_signal("继续")
    assert not _detect_frustration_signal("帮我看看天气")
    assert not _detect_frustration_signal(
        "Can you check the build for me?"
    )
    assert not _detect_frustration_signal("")
    assert not _detect_frustration_signal("   ")


def test_frustration_no_false_positive_on_partial_words() -> None:
    """Markers should not fire on innocuous substrings."""
    # "stop" alone doesn't fire — only "stop doing".
    assert not _detect_frustration_signal("Find the next stop.")
    # "wrong" alone doesn't fire — needs "that's wrong" frame.
    assert not _detect_frustration_signal(
        "Sort by least wrong answers first."
    )


# ── _sanitize_memory_context now strips curriculum-hint too ───────


def test_sanitizer_strips_curriculum_hint_block() -> None:
    """The hint rides on the user message in the live prompt; we must
    NOT persist it into long-term history. Otherwise the next turn's
    recall would see "[System note: ...]" framing as if the user had
    typed it."""
    raw = (
        "fix the bug please\n\n"
        "<curriculum-hint>\n"
        "[System note: the user's current message contains "
        "frustration / pushback signals. Two-step response:\n"
        "  1. FIRST, address the immediate request — do not "
        "lecture the user about the meta-process.\n"
        "  2. AFTER the immediate issue is resolved...]\n"
        "</curriculum-hint>"
    )
    out = _sanitize_memory_context(raw)
    assert out == "fix the bug please"
    assert "<curriculum-hint>" not in out
    assert "System note" not in out


def test_sanitizer_strips_orphan_curriculum_hint_tag() -> None:
    """Defensive: even if the close tag was lost, the open tag must
    not survive into history."""
    raw = "real user content<curriculum-hint>"
    out = _sanitize_memory_context(raw)
    assert out == "real user content"


def test_sanitizer_preserves_unrelated_content() -> None:
    """No-op when the input has no envelope blocks."""
    raw = "just a regular message with no envelopes"
    assert _sanitize_memory_context(raw) == raw
