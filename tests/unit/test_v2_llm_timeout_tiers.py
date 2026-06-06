"""AgentLoop._compute_llm_timeout — pin the per-call wall-clock tiers.

Regression target (2026-06-05): the original tiering short-circuited to
60s whenever ``tool_count > 0``. In XMclaw nearly every turn has tools
available, so the "complex task → full configured budget" branch was
effectively unreachable. A reasoning model working a genuinely hard task
(screenshot: "拉 432 个技能 ID 按命名空间分类") thinks well past 60s and
was aborted mid-stream with "LLM call exceeded 60s wall-clock at hop 1".

Root cause: tool *availability* is the wrong complexity signal — having
tools makes a turn MORE likely to be long-running, not less. The fix
stops using ``tool_count`` to lower the budget; only message shape and
images affect it.

``_compute_llm_timeout`` only reads ``self._llm_timeout_s``, so we call
it as an unbound method against a tiny stub instead of constructing a
full AgentLoop (which needs an LLM, tools, bus, …).
"""
from __future__ import annotations

from types import SimpleNamespace

from xmclaw.daemon.agent_loop import AgentLoop


def _timeout(
    *,
    message: str,
    upper_bound: float = 300.0,
    has_image: bool = False,
    tool_count: int = 0,
) -> float:
    stub = SimpleNamespace(_llm_timeout_s=upper_bound)
    return AgentLoop._compute_llm_timeout(
        stub,  # type: ignore[arg-type]
        user_message=message,
        has_image=has_image,
        tool_count=tool_count,
    )


# ── the actual regression ────────────────────────────────────────────


def test_complex_task_with_tools_gets_full_budget() -> None:
    """THE BUG: a complex task with tools available must NOT be capped
    at 60s. This is the screenshot case verbatim."""
    t = _timeout(
        message="哥，先把432个技能ID拉出来，按命名空间分类。",
        tool_count=120,  # tools available — used to force the 60s cap
    )
    assert t == 300.0, (
        f"complex tool task was capped to {t}s — the tool_count>0 "
        "short-circuit regressed"
    )


def test_tool_availability_alone_never_lowers_budget() -> None:
    """Even a mid-length neutral prompt with tools must get the full
    budget — tool_count must not be a downgrade signal anymore."""
    msg = "Please go through the repository and tell me what you find here."
    assert _timeout(message=msg, tool_count=99) == 300.0
    # And with no tools, same message, same result — tool_count is inert.
    assert _timeout(message=msg, tool_count=0) == 300.0


# ── trimming still works for trivial chatter ─────────────────────────


def test_short_greeting_is_trimmed() -> None:
    """A bare greeting shouldn't reserve the full 300s budget."""
    assert _timeout(message="你好", tool_count=50) == 60.0
    assert _timeout(message="hi there", tool_count=0) == 60.0


def test_short_but_worky_message_is_not_trimmed() -> None:
    """A SHORT message that is clearly a task (work verb) must still get
    the full budget — length alone must not trim real work."""
    assert _timeout(message="分析这个", tool_count=10) == 300.0
    assert _timeout(message="fix the bug", tool_count=10) == 300.0
    assert _timeout(message="重构 X", tool_count=0) == 300.0


def test_long_message_is_never_trimmed() -> None:
    """Anything over the short threshold gets the full budget regardless
    of content."""
    long_msg = "x" * 80  # > 50 chars, no work verb
    assert _timeout(message=long_msg) == 300.0


# ── images ───────────────────────────────────────────────────────────


def test_image_turn_gets_vision_tier() -> None:
    assert _timeout(message="look at this", has_image=True) == 120.0


def test_image_tier_respects_lower_upper_bound() -> None:
    """If the user configured a tighter upper bound, it always wins as
    the hard cap — even below the vision tier's nominal 120s."""
    assert _timeout(message="look", has_image=True, upper_bound=90.0) == 90.0


# ── upper bound is the hard ceiling everywhere ───────────────────────


def test_configured_upper_bound_is_hard_ceiling() -> None:
    """A low ``llm_timeout_s`` config must clamp every tier."""
    # Complex task, but user set a 45s ceiling → 45s.
    assert _timeout(
        message="分析整个代码库并生成报告" * 5,
        upper_bound=45.0,
        tool_count=20,
    ) == 45.0
    # Greeting with a 45s ceiling → min(60, 45) = 45.
    assert _timeout(message="hi", upper_bound=45.0) == 45.0
