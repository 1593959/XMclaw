"""Unit tests for GoalAnchor — runtime scaffolding so weak/short-ctx
models match Kimi-style long-horizon tool chains.

Tests cover:
  * should_anchor cadence (every N hops, skipping hop 0)
  * Output format (marker present, goal echoed, plan rendered, budget shown)
  * Tool history compression (tail-N detail + earlier-K compressed footer)
  * is_anchor_message detection on str / list-of-blocks content
  * Persistence sanitiser strips the anchor (turn_context._sanitize_memory_context)
"""
from __future__ import annotations

import pytest

from xmclaw.cognition.goal_anchor import (
    GOAL_ANCHOR_MARKER,
    GoalAnchorState,
    GoalAnchorTracker,
    is_anchor_message,
)


# ── should_anchor cadence ────────────────────────────────────────


@pytest.mark.parametrize("hop,expected", [
    (0, False), (1, False), (2, False), (3, False), (4, False),
    (5, True), (6, False), (7, False), (8, False), (9, False),
    (10, True), (15, True), (20, True), (100, True),
])
def test_should_anchor_every_5_default(hop, expected):
    t = GoalAnchorTracker(anchor_every=5)
    assert t.should_anchor(hop) is expected


def test_should_anchor_respects_custom_cadence():
    t3 = GoalAnchorTracker(anchor_every=3)
    assert [t3.should_anchor(h) for h in range(10)] == [
        False, False, False, True, False, False, True, False, False, True,
    ]


def test_should_anchor_anchor_every_clamped_to_1():
    """anchor_every <= 0 falls back to 1 (anchor every hop after 0)."""
    t = GoalAnchorTracker(anchor_every=0)
    assert t.should_anchor(0) is False
    assert t.should_anchor(1) is True
    assert t.should_anchor(2) is True


# ── Output format ────────────────────────────────────────────────


def test_format_includes_marker_and_goal():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="Find all TODOs in the codebase",
        hop=5,
        max_hops=30,
        tool_calls_made=[],
    ))
    assert out.startswith(GOAL_ANCHOR_MARKER)
    assert "Find all TODOs in the codebase" in out
    # Budget line present
    assert "hop 5 / 30" in out
    assert "剩余 25" in out


def test_format_renders_plan_with_completion_marks():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="g",
        hop=5,
        max_hops=20,
        tool_calls_made=[],
        plan_steps=["grep TODOs", "read files", "aggregate report"],
        completed_step_indices={0, 1},
    ))
    assert "[x] 1. grep TODOs" in out
    assert "[x] 2. read files" in out
    assert "[ ] 3. aggregate report" in out


def test_format_compresses_old_tool_calls():
    """When > tail_calls_summary tools made, header lists how many were
    compressed and the tail keeps detail."""
    t = GoalAnchorTracker(tail_calls_summary=3)
    tools = [
        {"name": f"tool_{i}", "ok": True, "content_preview": f"r{i}"}
        for i in range(10)
    ]
    out = t.format(GoalAnchorState(
        original_goal="g", hop=5, max_hops=50,
        tool_calls_made=tools,
    ))
    # Compressed header
    assert "earlier 7 calls compressed" in out
    # Last 3 visible
    assert "tool_7" in out and "tool_8" in out and "tool_9" in out
    # Earlier ones NOT shown line-by-line
    assert "tool_0" not in out
    assert "tool_5" not in out


def test_format_shows_open_errors():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="g", hop=5, max_hops=20,
        tool_calls_made=[],
        open_errors=["permission denied", "file not found"],
    ))
    assert "permission denied" in out
    assert "file not found" in out
    assert "open errors" in out.lower()


def test_format_tool_fail_shows_error():
    """Failed tools show their error, not their content_preview."""
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="g", hop=5, max_hops=20,
        tool_calls_made=[
            {"name": "file_write", "ok": False,
             "error": "permission denied on /etc/passwd"},
        ],
    ))
    assert "permission denied on /etc/passwd" in out


def test_format_truncates_long_goal():
    """Massive original_goal is capped — anchor must fit in a reasonable
    chunk of the context window."""
    t = GoalAnchorTracker()
    long_goal = "do " * 1000  # 3000 chars
    out = t.format(GoalAnchorState(
        original_goal=long_goal, hop=5, max_hops=20,
        tool_calls_made=[],
    ))
    # Goal section is capped at 800 chars + '…' marker
    assert "…" in out


# ── is_anchor_message ────────────────────────────────────────────


def test_is_anchor_message_str():
    assert is_anchor_message(GOAL_ANCHOR_MARKER + " hello") is True
    assert is_anchor_message("plain text") is False
    assert is_anchor_message("") is False


def test_is_anchor_message_block_list():
    """Provider-shape content: list of {type, text} blocks."""
    assert is_anchor_message([
        {"type": "text", "text": GOAL_ANCHOR_MARKER + " ..."},
    ]) is True
    assert is_anchor_message([
        {"type": "text", "text": "regular reply"},
    ]) is False


def test_is_anchor_message_handles_objects():
    """Block with .text attribute (not dict shape)."""

    class Block:
        def __init__(self, text):
            self.text = text

    assert is_anchor_message([Block(GOAL_ANCHOR_MARKER + " ...")]) is True
    assert is_anchor_message([Block("plain")]) is False


# ── Persistence sanitiser strips anchors ─────────────────────────


def test_sanitize_memory_context_strips_goal_anchor():
    """Anchor messages MUST NOT survive to on-disk history."""
    from xmclaw.daemon.turn_context import _sanitize_memory_context

    raw = (
        "User real text\n"
        + GOAL_ANCHOR_MARKER
        + " refreshed every 5 hops — ...\n\n"
        + "## Original Goal\nFind all TODOs\n"
    )
    out = _sanitize_memory_context(raw)
    assert "User real text" in out
    assert GOAL_ANCHOR_MARKER not in out
    assert "Find all TODOs" not in out  # whole anchor block stripped


def test_sanitize_memory_context_keeps_clean_text():
    from xmclaw.daemon.turn_context import _sanitize_memory_context
    out = _sanitize_memory_context("just a regular user message")
    assert out == "just a regular user message"
