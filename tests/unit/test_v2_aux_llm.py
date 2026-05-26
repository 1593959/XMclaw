"""Tests for the auxiliary-LLM resolver.

Locks in the routing: aux tasks prefer ``fast`` → ``balanced`` →
the supplied main LLM. Cache attribute on the registry prevents
re-walking the profile list every call.
"""
from __future__ import annotations

from xmclaw.daemon.aux_llm import resolve_aux_llm


class _FakeLLM:
    def __init__(self, model: str) -> None:
        self.model = model


class _FakeProfile:
    def __init__(self, model: str, tier: str) -> None:
        self.llm = _FakeLLM(model)
        self.tier = tier


class _FakeRegistry:
    """Mimics the LLMRegistry surface aux_llm uses."""

    def __init__(self, profiles: list[_FakeProfile]) -> None:
        self._profiles = profiles

    def by_tier(self, tier: str) -> list[_FakeProfile]:
        return [p for p in self._profiles if p.tier == tier]

    def pick_by_tier(self, tier, *, fallback_chain=()):
        hits = self.by_tier(tier)
        if hits:
            return hits[0]
        for fb in fallback_chain:
            hits = self.by_tier(fb)
            if hits:
                return hits[0]
        return None


def test_picks_fast_tier_when_available() -> None:
    main = _FakeLLM("flagship-pro")
    reg = _FakeRegistry([
        _FakeProfile("flash-mini", tier="fast"),
        _FakeProfile("balanced-x", tier="balanced"),
    ])
    aux = resolve_aux_llm(reg, main)
    assert aux.model == "flash-mini"


def test_falls_back_to_balanced_when_no_fast() -> None:
    main = _FakeLLM("flagship-pro")
    reg = _FakeRegistry([
        _FakeProfile("balanced-x", tier="balanced"),
    ])
    aux = resolve_aux_llm(reg, main)
    assert aux.model == "balanced-x"


def test_falls_back_to_main_when_no_fast_or_balanced() -> None:
    main = _FakeLLM("flagship-pro")
    reg = _FakeRegistry([
        _FakeProfile("strong-y", tier="strong"),
    ])
    aux = resolve_aux_llm(reg, main)
    assert aux is main


def test_falls_back_to_main_when_no_registry() -> None:
    main = _FakeLLM("flagship-pro")
    assert resolve_aux_llm(None, main) is main


def test_returns_none_when_both_missing() -> None:
    assert resolve_aux_llm(None, None) is None


def test_caches_resolution_on_registry() -> None:
    """Second call must NOT re-walk pick_by_tier. We stub the
    method to count calls and check it fires once."""
    main = _FakeLLM("flagship-pro")
    reg = _FakeRegistry([_FakeProfile("flash-mini", tier="fast")])
    calls = 0
    real_pick = reg.pick_by_tier

    def counted_pick(*a, **kw):
        nonlocal calls
        calls += 1
        return real_pick(*a, **kw)
    reg.pick_by_tier = counted_pick  # type: ignore[assignment]

    a1 = resolve_aux_llm(reg, main)
    a2 = resolve_aux_llm(reg, main)
    a3 = resolve_aux_llm(reg, main)
    assert a1 is a2 is a3
    assert calls == 1, f"expected 1 pick_by_tier call, got {calls}"


def test_picks_first_fast_when_multiple_registered() -> None:
    """Insertion order wins — matches LLMRegistry.by_tier contract."""
    main = _FakeLLM("flagship-pro")
    reg = _FakeRegistry([
        _FakeProfile("first-fast", tier="fast"),
        _FakeProfile("second-fast", tier="fast"),
    ])
    aux = resolve_aux_llm(reg, main)
    assert aux.model == "first-fast"
