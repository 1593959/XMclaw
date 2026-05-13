"""Unit tests for ModelTierRouter (Sprint 0)."""
from __future__ import annotations

import pytest

from xmclaw.cognition.model_tier_router import ModelTierRouter, TierDecision


@pytest.fixture
def router():
    return ModelTierRouter()


# ── Vision tier (image attachments) ─────────────────────────────────


def test_image_attachment_routes_to_vision(router):
    d = router.route("分析这张截图", has_images=True)
    assert d.tier == "vision"
    assert d.has_images is True
    assert "balanced" in d.fallback_chain


def test_image_with_simple_question_still_vision(router):
    d = router.route("hi", has_images=True)
    assert d.tier == "vision"


# ── Strong tier (multi-step reasoning) ──────────────────────────────


@pytest.mark.parametrize("msg", [
    "First find all the TODOs, then summarise each one for review.",
    "Compare A and B side-by-side; analyse pros and cons of each.",
    "Design an architecture for the new auth flow.",
    "Refactor the agent loop to be more modular.",
    "首先帮我搜索一下相关文件，然后逐个分析。",
    "分析整个项目的架构，然后给出重构方案。",
])
def test_complex_message_routes_to_strong(router, msg):
    d = router.route(msg)
    assert d.tier == "strong"
    assert d.is_complex is True


def test_very_long_message_routes_to_strong(router):
    msg = "Please help me with " + "details " * 70
    d = router.route(msg)
    assert d.tier == "strong"


# ── Fast tier (trivial chitchat) ────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "hi",
    "Hello!",
    "你好",
    "thanks",
    "谢谢",
    "ok",
    "yes",
    "对",
    "几点了",
    "what time is it",
    "who are you",
    "你是谁",
])
def test_trivial_routes_to_fast(router, msg):
    d = router.route(msg)
    assert d.tier == "fast", f"expected fast for {msg!r}, got {d.tier} ({d.reason})"
    assert d.is_trivial is True


def test_short_message_routes_to_fast(router):
    d = router.route("早安")
    assert d.tier == "fast"


# ── Balanced tier (default + tool cues) ─────────────────────────────


@pytest.mark.parametrize("msg", [
    "search for this on the web",
    "read README.md and tell me",
    "run npm test",
    "查找 TODO 注释",
    "把这个截图发到微信群里",
])
def test_tool_cues_route_to_balanced(router, msg):
    d = router.route(msg)
    assert d.tier == "balanced", f"expected balanced for {msg!r}, got {d.tier}"
    assert d.has_tool_cues is True


def test_unclassified_routes_to_balanced(router):
    d = router.route("Write a paragraph about the weather.")
    # No tool, not trivial, not complex
    assert d.tier == "balanced"


# ── Forced override ─────────────────────────────────────────────────


def test_forced_overrides_heuristic(router):
    d = router.route("hi", forced_tier="strong")
    assert d.tier == "strong"


def test_forced_invalid_falls_back_to_heuristic(router):
    d = router.route("hi", forced_tier="bogus_tier")
    assert d.tier == "fast"  # heuristic prevails


# ── Edge cases ──────────────────────────────────────────────────────


def test_empty_message_balanced(router):
    d = router.route("")
    assert d.tier == "balanced"


def test_none_message_balanced(router):
    d = router.route(None)  # type: ignore[arg-type]
    assert d.tier == "balanced"


# ── Fallback chains ─────────────────────────────────────────────────


def test_vision_fallback_chain(router):
    d = router.route("hi", has_images=True)
    assert d.fallback_chain == ("balanced", "strong")


def test_fast_fallback_chain(router):
    d = router.route("hi")
    assert d.fallback_chain == ("balanced",)


def test_strong_fallback_chain(router):
    d = router.route(
        "Compare these three approaches in detail and design the best.",
    )
    assert d.fallback_chain == ("balanced",)


# ── LLMRegistry tier-based picking ──────────────────────────────────


def test_registry_pick_by_tier_finds_matching():
    from xmclaw.daemon.llm_registry import LLMProfile, LLMRegistry

    class _Stub:
        pass

    fast_prof = LLMProfile("p1", "fast-1", "openai", "haiku", _Stub(), tier="fast")  # type: ignore[arg-type]
    bal_prof = LLMProfile("p2", "bal-1", "openai", "sonnet", _Stub(), tier="balanced")  # type: ignore[arg-type]
    strong_prof = LLMProfile("p3", "strong-1", "anthropic", "opus", _Stub(), tier="strong")  # type: ignore[arg-type]
    registry = LLMRegistry(
        profiles={"p1": fast_prof, "p2": bal_prof, "p3": strong_prof},
        default_id="p2",
    )

    assert registry.pick_by_tier("fast") is fast_prof
    assert registry.pick_by_tier("balanced") is bal_prof
    assert registry.pick_by_tier("strong") is strong_prof


def test_registry_pick_by_tier_uses_fallback():
    from xmclaw.daemon.llm_registry import LLMProfile, LLMRegistry

    class _Stub:
        pass

    bal_prof = LLMProfile("p2", "bal-1", "openai", "sonnet", _Stub(), tier="balanced")  # type: ignore[arg-type]
    registry = LLMRegistry(
        profiles={"p2": bal_prof},
        default_id="p2",
    )

    # No "vision" tier configured — fall back to balanced
    picked = registry.pick_by_tier("vision", fallback_chain=("balanced",))
    assert picked is bal_prof


def test_registry_pick_by_tier_empty_returns_default():
    from xmclaw.daemon.llm_registry import LLMRegistry

    registry = LLMRegistry(profiles={}, default_id=None)
    assert registry.pick_by_tier("fast") is None


# ── factory tier inference ──────────────────────────────────────────


@pytest.mark.parametrize("model,expected_tier", [
    ("claude-haiku-4-5", "fast"),
    ("gpt-4o-mini", "fast"),
    ("qwen-7b-instruct", "fast"),
    ("claude-opus-4-7", "strong"),
    ("gpt-4.1", "strong"),
    ("kimi-k2-1m", "strong"),
    ("deepseek-r1", "strong"),
    ("claude-sonnet-4-6", "vision"),
    ("gpt-4o", "vision"),
    ("ui-tars-7b", "vision"),
    ("", "balanced"),
    ("some-unknown-model", "balanced"),
])
def test_infer_tier_from_model(model, expected_tier):
    from xmclaw.daemon.factory import _infer_tier_from_model
    assert _infer_tier_from_model(model) == expected_tier
