"""Sprint 3 #5 follow-up — factory wiring for evolution components.

Covers the three builder factories appended to ``xmclaw/daemon/factory.py``:

* :func:`build_strategy_bank_from_config` — gated on
  ``evolution.reasoning_bank.enabled``; needs both a memory store and
  an embedder.
* :func:`build_strategy_distiller_from_config` — gated on
  ``evolution.reasoning_bank.enabled``; needs an LLM. Tier flows from
  ``llm.model`` via :func:`classify_model_tier`.
* :func:`build_reflective_mutator_from_config` — gated on
  ``evolution.reflective_mutator.enabled``; needs an LLM. Same tier
  classification.

Plus a single end-to-end check that ``build_agent_from_config`` actually
threads the StrategyBank into the AgentLoop (the production
deliverable — without this the modules ship inert).

All tests use minimal stand-ins — a fake embedder with the structural
shape the real builder calls into, a fake memory store, and a mock LLM
with a ``.model`` attribute. No SQLite / network / real provider touch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.evolution.reflective_mutator import ReflectiveMutator
from xmclaw.core.journal.strategy_bank import StrategyBank
from xmclaw.core.journal.strategy_distiller import StrategyDistiller
from xmclaw.daemon.factory import (
    build_agent_from_config,
    build_reflective_mutator_from_config,
    build_strategy_bank_from_config,
    build_strategy_distiller_from_config,
)


# ── shared fakes ─────────────────────────────────────────────────────


class _FakeEmbedder:
    """Minimal embedder structural twin — async embed(list[str]) -> list[list[float]]."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.1, 0.2] for _ in texts]


class _FakeLLM:
    """Stand-in LLM — only the ``.model`` attribute is consulted by the
    factory functions under test (the distiller / mutator only call
    ``.complete`` / ``.acomplete`` at runtime, not at construction)."""

    def __init__(self, model: str) -> None:
        self.model = model


class _FakeMemory:
    """Fake SqliteVecMemory — accepted by StrategyBank's structural
    typing. We never actually call into it in these tests; we only need
    the constructor of StrategyBank to accept it without complaint.
    """


# ── build_strategy_bank_from_config ─────────────────────────────────


def test_strategy_bank_returns_none_by_default() -> None:
    """No evolution section → bank stays off (default-disabled)."""
    assert (
        build_strategy_bank_from_config(
            {}, memory=_FakeMemory(), embedder=_FakeEmbedder(),
        )
        is None
    )


def test_strategy_bank_returns_none_when_explicitly_disabled() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": False}}}
    assert (
        build_strategy_bank_from_config(
            cfg, memory=_FakeMemory(), embedder=_FakeEmbedder(),
        )
        is None
    )


def test_strategy_bank_returns_none_when_memory_missing() -> None:
    """Enabled but no memory store → can't build, return None."""
    cfg = {"evolution": {"reasoning_bank": {"enabled": True}}}
    assert (
        build_strategy_bank_from_config(
            cfg, memory=None, embedder=_FakeEmbedder(),
        )
        is None
    )


def test_strategy_bank_returns_none_when_embedder_missing() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": True}}}
    assert (
        build_strategy_bank_from_config(
            cfg, memory=_FakeMemory(), embedder=None,
        )
        is None
    )


def test_strategy_bank_built_when_all_wired() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": True}}}
    bank = build_strategy_bank_from_config(
        cfg, memory=_FakeMemory(), embedder=_FakeEmbedder(),
    )
    assert isinstance(bank, StrategyBank)


# ── build_strategy_distiller_from_config ────────────────────────────


def test_distiller_returns_none_by_default() -> None:
    assert build_strategy_distiller_from_config({}, llm=_FakeLLM("x")) is None


def test_distiller_returns_none_when_disabled() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": False}}}
    assert build_strategy_distiller_from_config(cfg, llm=_FakeLLM("x")) is None


def test_distiller_returns_none_when_no_llm() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": True}}}
    assert build_strategy_distiller_from_config(cfg, llm=None) is None


def test_distiller_picks_strong_tier_for_claude_sonnet_4() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": True}}}
    d = build_strategy_distiller_from_config(
        cfg, llm=_FakeLLM("claude-sonnet-4-20250514"),
    )
    assert isinstance(d, StrategyDistiller)
    # _tier is the private slot the distiller stores after validation.
    assert d._tier == "strong"  # noqa: SLF001 — internal state under test


def test_distiller_picks_weak_tier_for_gpt5_mini() -> None:
    cfg = {"evolution": {"reasoning_bank": {"enabled": True}}}
    d = build_strategy_distiller_from_config(
        cfg, llm=_FakeLLM("gpt-5-mini"),
    )
    assert isinstance(d, StrategyDistiller)
    assert d._tier == "weak"  # noqa: SLF001


def test_distiller_honours_max_strategies_override() -> None:
    cfg = {
        "evolution": {
            "reasoning_bank": {"enabled": True, "max_strategies": 3},
        },
    }
    d = build_strategy_distiller_from_config(
        cfg, llm=_FakeLLM("claude-sonnet-4"),
    )
    assert isinstance(d, StrategyDistiller)
    assert d._max == 3  # noqa: SLF001


# ── build_reflective_mutator_from_config ────────────────────────────


def test_mutator_returns_none_by_default() -> None:
    assert build_reflective_mutator_from_config({}, llm=_FakeLLM("x")) is None


def test_mutator_returns_none_when_disabled() -> None:
    cfg = {"evolution": {"reflective_mutator": {"enabled": False}}}
    assert build_reflective_mutator_from_config(cfg, llm=_FakeLLM("x")) is None


def test_mutator_returns_none_when_no_llm() -> None:
    cfg = {"evolution": {"reflective_mutator": {"enabled": True}}}
    assert build_reflective_mutator_from_config(cfg, llm=None) is None


def test_mutator_picks_strong_tier_for_claude_sonnet_4() -> None:
    cfg = {"evolution": {"reflective_mutator": {"enabled": True}}}
    m = build_reflective_mutator_from_config(
        cfg, llm=_FakeLLM("claude-sonnet-4-20250514"),
    )
    assert isinstance(m, ReflectiveMutator)
    assert m._tier == "strong"  # noqa: SLF001


def test_mutator_picks_weak_tier_for_gpt5_mini() -> None:
    cfg = {"evolution": {"reflective_mutator": {"enabled": True}}}
    m = build_reflective_mutator_from_config(
        cfg, llm=_FakeLLM("gpt-5-mini"),
    )
    assert isinstance(m, ReflectiveMutator)
    assert m._tier == "weak"  # noqa: SLF001


def test_mutator_honours_max_per_skill_override() -> None:
    cfg = {
        "evolution": {
            "reflective_mutator": {"enabled": True, "max_per_skill": 2},
        },
    }
    m = build_reflective_mutator_from_config(
        cfg, llm=_FakeLLM("claude-sonnet-4"),
    )
    assert isinstance(m, ReflectiveMutator)
    assert m._max_per_skill == 2  # noqa: SLF001


# ── end-to-end: build_agent_from_config wires the bank ───────────────


def test_agent_factory_wires_strategy_bank_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliverable: factory stitches StrategyBank into the AgentLoop.

    Pre-Sprint-3 #5-follow-up the modules existed but no production
    caller ever instantiated them — this test pins the wiring so a
    future refactor can't silently regress to inert.

    StrategyBank needs both a SqliteVecMemory and an embedder. The
    factory builds the memory store from cfg and resolves the embedder
    via :func:`build_embedding_provider`; we stub the latter so the test
    doesn't need OpenAI / Ollama keys to exercise the wiring contract.
    """
    # Pin XMclaw runtime data away from the user's real ``~/.xmclaw``.
    monkeypatch.setenv("XMC_HOME", str(tmp_path))

    # Force the factory's embedder lookup to return our fake — every
    # other code path in build_agent_from_config goes through the same
    # symbol, so a single monkeypatch covers both the BuiltinTools.set_
    # embedder branch (B-40) and the agent_embedder branch (B-55).
    from xmclaw.providers.memory import embedding as _emb_mod
    monkeypatch.setattr(
        _emb_mod, "build_embedding_provider", lambda _cfg: _FakeEmbedder(),
    )

    bus = InProcessEventBus()
    cfg = {
        "llm": {
            "anthropic": {
                "api_key": "sk-ant-test",
                "default_model": "claude-sonnet-4-20250514",
            },
        },
        "evolution": {"reasoning_bank": {"enabled": True}},
    }
    agent = build_agent_from_config(cfg, bus)
    assert agent is not None
    bank: Any = agent._strategy_bank  # noqa: SLF001 — wiring under test
    assert isinstance(bank, StrategyBank)


def test_agent_factory_leaves_strategy_bank_none_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default config (no reasoning_bank) → AgentLoop runs without bank.

    Guards the "additive, off by default" property — turning the feature
    on must never become the implicit default for users on stable
    releases.
    """
    monkeypatch.setenv("XMC_HOME", str(tmp_path))
    bus = InProcessEventBus()
    cfg = {
        "llm": {
            "anthropic": {
                "api_key": "sk-ant-test",
                "default_model": "claude-sonnet-4-20250514",
            },
        },
    }
    agent = build_agent_from_config(cfg, bus)
    assert agent is not None
    assert agent._strategy_bank is None  # noqa: SLF001
