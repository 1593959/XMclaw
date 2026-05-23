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


# ── Wave-27 fix-7: session goal renders above current-turn ────────


def test_session_goal_shows_above_current_turn_when_different():
    """When session_goal != current original_goal, the anchor must
    render BOTH — the user's opening ask AND what they just said.
    Solves the "聊着聊着就忘了最初的目的" multi-turn drift case.
    """
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="实际上,我现在想改一下颜色",       # current turn
        session_goal="帮我搭一个陪玩店管理后台",          # session opening
        hop=5,
        max_hops=30,
        tool_calls_made=[],
    ))
    # Both labels appear, session goal comes first (above).
    assert "会话最初目标" in out
    assert "当前回合输入" in out
    assert "帮我搭一个陪玩店管理后台" in out
    assert "实际上,我现在想改一下颜色" in out
    # Session goal appears physically BEFORE current-turn in the text.
    assert out.index("帮我搭一个陪玩店管理后台") < out.index(
        "实际上,我现在想改一下颜色"
    )


def test_session_goal_collapses_when_same_as_original():
    """Turn 1 case: session_goal == current turn message → only one
    block rendered, no token waste on duplication.
    """
    t = GoalAnchorTracker()
    msg = "帮我搭一个陪玩店管理后台"
    out = t.format(GoalAnchorState(
        original_goal=msg,
        session_goal=msg,
        hop=5,
        max_hops=30,
        tool_calls_made=[],
    ))
    assert "原始目标" in out                  # legacy single-goal label
    assert "会话最初目标" not in out          # split-block label absent
    assert "当前回合输入" not in out
    assert out.count(msg) == 1                # not duplicated


def test_session_goal_none_falls_back_to_original():
    """Backward compat: when session_goal is None, behavior matches
    the pre-fix-7 single-goal rendering exactly."""
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="some task",
        session_goal=None,
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    assert "原始目标" in out
    assert "会话最初目标" not in out


# ── Wave-27 fix-8: full user thread (B) ────────────────────────────


def test_session_user_thread_renders_as_numbered_evolution():
    """The full chain of user asks renders as a numbered list — the
    LLM sees the EVOLUTION of intent, not a frozen snapshot.

    The test uses an exact-match final entry to verify the
    drop-duplicate-of-current-turn logic. (Substring overlap is
    benign — we only dedupe EXACT matches.)
    """
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="复刻你的逻辑",
        session_user_thread=[
            "压缩太快了",
            "卡太死了, 别 hardcode minimax",
            "长任务忘了目的, 重新设计",
            "复刻你的逻辑",     # exact match — gets dropped
        ],
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    assert "用户提出过的诉求" in out
    assert "1. 压缩太快了" in out
    assert "2. 卡太死了" in out
    assert "3. 长任务忘了目的, 重新设计" in out
    # The exact-match final "复刻你的逻辑" was dropped from the
    # thread; it should appear once (as the current turn) not twice.
    assert out.count("复刻你的逻辑") == 1


def test_session_user_thread_dedupes_consecutive_repeats():
    """User repeating themselves verbatim shouldn't bloat the
    anchor."""
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="something new",
        session_user_thread=[
            "first ask",
            "first ask",       # consecutive dupe — dropped
            "second ask",
            "second ask",      # consecutive dupe — dropped
        ],
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    assert out.count("first ask") == 1
    assert out.count("second ask") == 1


def test_session_user_thread_caps_at_12():
    """Bound the anchor budget: keep the most recent 12 asks."""
    t = GoalAnchorTracker()
    huge_thread = [f"ask-{i}" for i in range(25)]
    out = t.format(GoalAnchorState(
        original_goal="current",
        session_user_thread=huge_thread,
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    # ask-0..ask-12 dropped (older), ask-13..ask-24 kept.
    assert "ask-0\n" not in out
    assert "ask-12" not in out      # dropped (oldest of the cap)
    assert "ask-13" in out          # kept
    assert "ask-24" in out          # kept (most recent)


def test_session_user_thread_single_entry_collapses_to_legacy_render():
    """Thread of 1 entry == legacy session_goal case; don't render
    the numbered list (would just be '1. <text>')."""
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="now",
        session_user_thread=["original"],
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    assert "用户提出过的诉求" not in out
    # Legacy single-block rendering still works.
    assert "now" in out


# ── Wave-27 fix-8 / C: agent-self-declared current_focus ──────────


def test_current_focus_renders_above_user_thread():
    """When the agent has called update_focus, the declared focus
    appears AT THE TOP of the anchor — most-recent agent intent.

    Thread has at least 2 unique entries that survive dedup so the
    user-thread block renders (not the legacy single-block path).
    """
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="latest user input",
        current_focus="重新设计 token-budget 驱动的压缩",
        session_user_thread=[
            "original ask", "middle ask", "latest user input",
        ],
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    assert "当前焦点" in out
    assert "重新设计 token-budget 驱动的压缩" in out
    assert "用户提出过的诉求" in out
    # Focus appears before everything else in the body.
    assert out.index("当前焦点") < out.index("用户提出过的诉求")


def test_current_focus_omitted_when_unset():
    """No focus → no block (no empty header pollution)."""
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="task",
        current_focus=None,
        hop=5, max_hops=30, tool_calls_made=[],
    ))
    assert "当前焦点" not in out


# ── set_session_focus / get_session_focus registry ────────────────


def test_session_focus_registry_round_trip():
    from xmclaw.cognition.goal_anchor import (
        set_session_focus, get_session_focus, _reset_session_focus_for_tests,
    )
    _reset_session_focus_for_tests()
    assert get_session_focus("s1") is None
    set_session_focus("s1", "doing the thing")
    assert get_session_focus("s1") == "doing the thing"
    # Overwrite.
    set_session_focus("s1", "doing a different thing")
    assert get_session_focus("s1") == "doing a different thing"
    # Clear via empty.
    set_session_focus("s1", "")
    assert get_session_focus("s1") is None
    # Empty session_id is a no-op.
    set_session_focus("", "x")
    assert get_session_focus("") is None


def test_session_focus_registry_isolates_sessions():
    from xmclaw.cognition.goal_anchor import (
        set_session_focus, get_session_focus, _reset_session_focus_for_tests,
    )
    _reset_session_focus_for_tests()
    set_session_focus("a", "focus A")
    set_session_focus("b", "focus B")
    assert get_session_focus("a") == "focus A"
    assert get_session_focus("b") == "focus B"


# ── Jarvis Phase 6.3: skill_matches rendering ────────────────────────────


def test_format_renders_skill_matches():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="deploy my app",
        hop=5, max_hops=30, tool_calls_made=[],
        skill_matches=[
            {"skill_id": "deploy-vercel", "version": 1,
             "title": "Deploy to Vercel"},
            {"skill_id": "deploy-aws", "version": 2,
             "title": "Deploy to AWS"},
        ],
    ))
    assert "已匹配技能 (Matched skills" in out
    assert "skill_deploy-vercel (v1) — Deploy to Vercel" in out
    assert "skill_deploy-aws (v2) — Deploy to AWS" in out


def test_format_omits_skill_matches_when_empty():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="deploy my app",
        hop=5, max_hops=30, tool_calls_made=[],
        skill_matches=[],
    ))
    assert "已匹配技能" not in out


def test_format_omits_skill_matches_when_none():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="deploy my app",
        hop=5, max_hops=30, tool_calls_made=[],
        skill_matches=None,
    ))
    assert "已匹配技能" not in out


def test_format_skill_match_without_title():
    t = GoalAnchorTracker()
    out = t.format(GoalAnchorState(
        original_goal="deploy my app",
        hop=5, max_hops=30, tool_calls_made=[],
        skill_matches=[
            {"skill_id": "foo", "version": 1, "title": ""},
        ],
    ))
    assert "skill_foo (v1)" in out
    # No stray " — " when title is blank.
    assert "skill_foo (v1) —" not in out


def test_format_skill_matches_capped_at_five():
    t = GoalAnchorTracker()
    matches = [
        {"skill_id": f"skill-{i}", "version": 1, "title": f"Title {i}"}
        for i in range(8)
    ]
    out = t.format(GoalAnchorState(
        original_goal="deploy my app",
        hop=5, max_hops=30, tool_calls_made=[],
        skill_matches=matches,
    ))
    # All 8 should appear because we only filter in display loop... wait,
    # let me check the code. Yes, `[:5]` is used.
    assert out.count("skill_skill-") == 5
