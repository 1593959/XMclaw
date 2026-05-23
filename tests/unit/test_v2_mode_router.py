"""Unit tests for ModeRouter (Batch D)."""
from __future__ import annotations

import pytest

from xmclaw.cognition.mode_router import (
    ModeRouter,
    RouteDecision,
    RunMode,
    _coerce_mode,
)


# ── Greetings + trivial → instant ───────────────────────────────


@pytest.mark.parametrize("msg", [
    "hi",
    "Hello!",
    "hey",
    "你好",
    "嗨",
    "good morning",
    "thanks",
    "thank you",
    "谢谢",
    "ok",
    "OK",
    "明白",
    "yes",
    "no",
    "对",
    "什么时间",
    "现在几点",
    "今天星期几",
    "what time is it",
    "who are you",
    "你是谁",
])
def test_routes_trivial_to_instant(msg):
    router = ModeRouter()
    decision = router.route(msg)
    assert decision.mode == RunMode.INSTANT, (
        f"expected instant for {msg!r}, got {decision.mode}: {decision.reason}"
    )


# ── Tool cues → agent even if greeting-like ─────────────────────


@pytest.mark.parametrize("msg", [
    "hi, please search for foo",
    "hello, can you read README.md",
    "thanks for running the tests",
    "hi, deploy this to staging please",
])
def test_greeting_with_tool_cue_routes_to_agent(msg):
    router = ModeRouter()
    decision = router.route(msg)
    assert decision.mode == RunMode.AGENT


# ── Default → agent ─────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "Refactor the agent loop to be more modular",
    "Fix the bug in retry_aware.py around line 200",
    "Update the docs",
    "把代码改一改",
])
def test_default_routes_to_agent(msg):
    router = ModeRouter()
    decision = router.route(msg)
    assert decision.mode == RunMode.AGENT


# ── Thinking cues ───────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "Think out loud about this architecture choice",
    "Reason through the tradeoffs of event-sourcing here",
    "Walk me through your reasoning for the choice",
    "explain your thinking",
    "深入思考一下这个问题",
    "解释你的推理过程",
])
def test_routes_thinking_cues_to_thinking(msg):
    router = ModeRouter()
    decision = router.route(msg)
    assert decision.mode == RunMode.THINKING


# ── Swarm cues (only when enabled) ──────────────────────────────


@pytest.mark.parametrize("msg", [
    "Compare these three options side-by-side",
    "Summarise each of the following files",
    "Analyse each function in parallel",
    "For each of these tickets, generate a summary",
    "分别总结这几个文件",
    "并行分析这三个模块",
    "逐一分析下面这些方法",
])
def test_swarm_cues_when_enabled(msg):
    router = ModeRouter(enable_swarm=True)
    decision = router.route(msg)
    assert decision.mode == RunMode.SWARM


def test_swarm_cues_demoted_when_disabled():
    router = ModeRouter(enable_swarm=False)
    decision = router.route("Compare these three options side-by-side")
    assert decision.mode == RunMode.AGENT  # falls back


# ── Forced mode ─────────────────────────────────────────────────


def test_forced_mode_overrides_heuristic():
    router = ModeRouter()
    # "hi" would route to instant, but forced=agent wins.
    decision = router.route("hi", forced_mode=RunMode.AGENT)
    assert decision.mode == RunMode.AGENT
    assert decision.forced is True


def test_forced_mode_string():
    router = ModeRouter()
    decision = router.route("anything", forced_mode="thinking")
    assert decision.mode == RunMode.THINKING


def test_forced_mode_alias():
    router = ModeRouter()
    decision = router.route("anything", forced_mode="quick")
    assert decision.mode == RunMode.INSTANT


def test_invalid_forced_mode_falls_back_to_heuristic():
    router = ModeRouter()
    decision = router.route("hi", forced_mode="not-a-mode")
    assert decision.mode == RunMode.INSTANT  # heuristic prevails


# ── Edge cases ──────────────────────────────────────────────────


def test_empty_message_default():
    router = ModeRouter()
    decision = router.route("")
    assert decision.mode == RunMode.AGENT


def test_none_message_default():
    router = ModeRouter()
    decision = router.route(None)  # type: ignore[arg-type]
    assert decision.mode == RunMode.AGENT


def test_long_message_with_greeting_prefix_routes_to_agent():
    """User pasted a 500-char essay starting with 'hi' — that's not a
    trivial chat; it's a task."""
    router = ModeRouter(min_chars_for_agent=200)
    long_body = "hi " + "lorem ipsum " * 50
    decision = router.route(long_body)
    # > 200 chars + has greeting → still agent (heuristic suppressed
    # the instant route).
    assert decision.mode == RunMode.AGENT


def test_instant_disabled_via_config():
    router = ModeRouter(enable_instant=False)
    decision = router.route("hi")
    # Without instant, even pure "hi" routes to agent.
    assert decision.mode == RunMode.AGENT


def test_custom_default_mode():
    router = ModeRouter(default_mode=RunMode.THINKING)
    decision = router.route("write some random code")
    assert decision.mode == RunMode.THINKING


# ── Helper / parser ────────────────────────────────────────────


@pytest.mark.parametrize("s,expected", [
    ("instant", RunMode.INSTANT),
    ("INSTANT", RunMode.INSTANT),
    ("thinking", RunMode.THINKING),
    ("agent", RunMode.AGENT),
    ("swarm", RunMode.SWARM),
    ("quick", RunMode.INSTANT),
    ("think", RunMode.THINKING),
    ("default", RunMode.AGENT),
    ("fanout", RunMode.SWARM),
    ("nonsense", None),
    ("", None),
])
def test_coerce_mode(s, expected):
    assert _coerce_mode(s) == expected


# ── Jarvis Phase 6.4: SWARM enabled by default ─────────────────


def test_swarm_enabled_by_default():
    """ModeRouter should detect swarm cues without explicit enable_swarm=True."""
    router = ModeRouter()  # default enable_swarm
    decision = router.route("compare these three options side-by-side")
    assert decision.mode == RunMode.SWARM


def test_swarm_can_be_explicitly_disabled():
    """When opt-out, swarm cues fall back to agent."""
    router = ModeRouter(enable_swarm=False)
    decision = router.route("compare these three options side-by-side")
    assert decision.mode == RunMode.AGENT
