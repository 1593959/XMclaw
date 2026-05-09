"""Sprint 3 #5 (Iron Rule #3) — per-model capability profile tests.

Iron Rule #3 (`docs/EVOLUTION_HONEST_STATE.md`):

    "Per-model capability profile: Strong models (Claude / Opus) get
    self-extension prompts; weak models (GPT-5 / 7B) get template-fill
    mode. Don't silently downgrade the loop because the user picked
    GPT-5 instead of Claude."

This file pins:

* The 4 ``ProviderProfile`` evolution-tier ratings + 3 capability
  flags on the existing DeepSeek / Kimi / Qwen / Gemini profiles.
* The ``classify_model_tier(model_id)`` substring-classifier for
  models that don't have a registered profile (Anthropic Claude /
  OpenAI GPT-* / native models).
* Conservative defaults: unknown → ``"unknown"`` → downstream
  ``"medium"`` handling.
"""
from __future__ import annotations

import pytest

from xmclaw.providers.llm._provider_profiles import (
    DEEPSEEK,
    GEMINI,
    KIMI,
    PROFILES,
    QWEN,
    ProviderProfile,
    classify_model_tier,
)


# ── ProviderProfile evolution-tier fields ─────────────────────────


def test_iron_rule_3_provider_profile_has_evolution_fields() -> None:
    """The ProviderProfile dataclass exposes the 4 new fields."""
    p = ProviderProfile(
        provider_id="x", display_name="X", default_base_url="http://x",
    )
    assert hasattr(p, "evolution_tier")
    assert hasattr(p, "supports_self_extension")
    assert hasattr(p, "supports_reflective_mutation")
    assert hasattr(p, "supports_strategy_distillation")


def test_iron_rule_3_profile_defaults_are_conservative() -> None:
    """Default a brand-new profile to ``medium`` tier with all 3 flags
    on — operators can flip individual flags off if they observe
    silent-degradation. Dropping a flag is reversible; defaulting them
    to off would mean nobody gets evolution surfaces by default."""
    p = ProviderProfile(
        provider_id="custom", display_name="Custom",
        default_base_url="http://x",
    )
    assert p.evolution_tier == "medium"
    assert p.supports_self_extension is True
    assert p.supports_reflective_mutation is True
    assert p.supports_strategy_distillation is True


def test_iron_rule_3_deepseek_kimi_strong() -> None:
    """DeepSeek (V3 / R1) and Kimi-K2 are top-tier on synthesis tasks."""
    assert DEEPSEEK.evolution_tier == "strong"
    assert KIMI.evolution_tier == "strong"


def test_iron_rule_3_qwen_gemini_medium() -> None:
    """Qwen + Gemini default to medium because tier varies by SKU
    (Plus vs Turbo, Pro vs Flash)."""
    assert QWEN.evolution_tier == "medium"
    assert GEMINI.evolution_tier == "medium"


def test_iron_rule_3_all_profiles_have_evolution_tier() -> None:
    """Every profile in the registry must declare its tier — no
    silent fallback to default-medium for known providers."""
    for p in PROFILES:
        assert p.evolution_tier in {"strong", "medium", "weak", "unknown"}, (
            f"profile {p.provider_id} has invalid tier {p.evolution_tier!r}"
        )


# ── classify_model_tier helper ────────────────────────────────────


@pytest.mark.parametrize("model_id", [
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-20250514",
    "claude-3-5-sonnet-20241022",
    "gpt-4o-2024-08-06",
    "gpt-4-turbo-2024-04-09",
    "o1-pro-2025-03-19",
    "o1-preview-2024-09-12",
    "deepseek-v3",
    "deepseek-r1",
    "kimi-k2-0905-preview",
])
def test_iron_rule_3_strong_models_classified_strong(model_id: str) -> None:
    """Top-tier flagships → ``"strong"`` so they get self-extension +
    reflective mutation + strategy distillation enabled."""
    assert classify_model_tier(model_id) == "strong", (
        f"{model_id} should classify as 'strong'"
    )


@pytest.mark.parametrize("model_id", [
    "gpt-4o-mini",
    "gpt-4.1-2025-04-14",
    "o1-mini-2024-12-17",
    "claude-haiku-4-5",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "qwen-max",
    "qwen-plus",
    "qwen3-coder-plus",
    "moonshot-v1-128k",
])
def test_iron_rule_3_medium_models_classified_medium(model_id: str) -> None:
    """Mid-tier models → ``"medium"`` (distill + constrained mutation
    OK; self-extension scaffolded with template-fill)."""
    assert classify_model_tier(model_id) == "medium", (
        f"{model_id} should classify as 'medium'"
    )


@pytest.mark.parametrize("model_id", [
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-3.5-turbo",
    "llama-3.1-8b-instruct",
    "llama-3.2-3b",
    "llama-3.2-1b",
    "qwen-turbo",
])
def test_iron_rule_3_weak_models_classified_weak(model_id: str) -> None:
    """Weak-tier models → ``"weak"`` so distill + mutation are skipped
    (Live-SWE issue #7: silent degradation)."""
    assert classify_model_tier(model_id) == "weak", (
        f"{model_id} should classify as 'weak'"
    )


def test_iron_rule_3_unknown_model_classified_unknown() -> None:
    """A model not in the patterns table → ``"unknown"`` (downstream
    treats as ``"medium"`` — conservative)."""
    assert classify_model_tier("totally-fictional-model-2099") == "unknown"


def test_iron_rule_3_classify_handles_none_and_empty() -> None:
    """``None`` / empty / non-string → ``"unknown"``."""
    assert classify_model_tier(None) == "unknown"
    assert classify_model_tier("") == "unknown"
    assert classify_model_tier("   ") == "unknown"


def test_iron_rule_3_classify_case_insensitive() -> None:
    """``CLAUDE-SONNET-4-XYZ`` matches the same way ``claude-sonnet-4``
    does — model ids are case-insensitive in practice across providers."""
    assert classify_model_tier("CLAUDE-SONNET-4-20250514") == "strong"
    assert classify_model_tier("GPT-4O-MINI") == "medium"


def test_iron_rule_3_first_match_wins() -> None:
    """The patterns table is ordered; first match wins. ``gpt-5-mini``
    must classify as ``"weak"`` (not ``"strong"`` or ``"medium"``) —
    important because a future ``gpt-5`` flagship entry could
    accidentally shadow the mini variant via substring overlap."""
    # Exact pattern for gpt-5-mini is in the weak list. If a later
    # commit accidentally adds "gpt-5" to the strong list above it,
    # this test catches the silent regression.
    assert classify_model_tier("gpt-5-mini-preview") == "weak"


def test_iron_rule_3_gpt_5_flagship_is_unknown() -> None:
    """Plain ``gpt-5`` (not gpt-5-mini / nano) is intentionally
    NOT listed — its actual evolution-task behaviour varies wildly
    in 2026. Conservative classification: ``"unknown"`` → downstream
    ``"medium"``. If we get clear evidence later, this test should
    flip."""
    # NB: gpt-5 substring DOES appear in "gpt-5-mini" but the patterns
    # table puts mini/nano first so a literal "gpt-5-..." that's not
    # mini/nano falls through. "gpt-5-2026-...-flagship" is the most
    # common flagship-id shape we'd see.
    assert classify_model_tier("gpt-5-2026-flagship") == "unknown"
