"""CostTracker — unit tests for anti-req #6 (hard budget circuit-breaker)."""
from __future__ import annotations

import pytest

from xmclaw.utils.cost import BudgetExceeded, CostTracker, Pricing


# ── record ──────────────────────────────────────────────────────────────


def test_record_known_model_charges_listed_price() -> None:
    t = CostTracker()
    # Haiku 4.5: 0.8 in, 4.0 out per million → 1M+1M = $4.80
    cost = t.record(
        "Anthropic", "claude-haiku-4-5-20251001",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )
    assert cost == pytest.approx(4.80, abs=1e-6)
    assert t.spent_usd == pytest.approx(4.80, abs=1e-6)


def test_record_unknown_model_costs_zero_by_default() -> None:
    """Unknown models shouldn't block — the tracker records usage but
    attributes zero cost unless the caller provides explicit pricing."""
    t = CostTracker()
    cost = t.record(
        "custom", "some-unknown-model",
        prompt_tokens=1000, completion_tokens=1000,
    )
    assert cost == 0.0
    assert len(t.ledger) == 1


def test_record_with_explicit_pricing_overrides_table() -> None:
    t = CostTracker()
    cost = t.record(
        "custom", "unknown",
        prompt_tokens=1_000_000, completion_tokens=0,
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=0.0),
    )
    assert cost == pytest.approx(2.0, abs=1e-6)


def test_record_accumulates_across_calls() -> None:
    t = CostTracker()
    t.record("A", "claude-haiku-4-5-20251001", 100_000, 0)  # $0.08
    t.record("A", "claude-haiku-4-5-20251001", 0, 100_000)  # $0.40
    assert t.spent_usd == pytest.approx(0.48, abs=1e-4)


def test_record_ledger_retains_every_entry() -> None:
    t = CostTracker()
    t.record("A", "claude-haiku-4-5-20251001", 100, 100)
    t.record("O", "gpt-4o", 100, 100)
    assert len(t.ledger) == 2
    assert t.ledger[0].provider == "A"
    assert t.ledger[1].provider == "O"


def test_record_rejects_negative_tokens() -> None:
    t = CostTracker()
    with pytest.raises(ValueError, match="negative token counts"):
        t.record("A", "m", prompt_tokens=-1, completion_tokens=0)


def test_pricing_overrides_at_construction() -> None:
    t = CostTracker(pricing_overrides={"my-model": Pricing(1.0, 1.0)})
    cost = t.record("x", "my-model", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.0, abs=1e-6)


# ── check_budget ────────────────────────────────────────────────────────


def test_check_budget_passes_when_under_cap() -> None:
    t = CostTracker(budget_usd=1.0)
    t.record("A", "claude-haiku-4-5-20251001", 1000, 1000)
    t.check_budget()  # well under $1


def test_check_budget_raises_when_over_cap() -> None:
    t = CostTracker(budget_usd=0.01)
    # $0.08 on 100k input tokens — over 0.01 cap
    t.record("A", "claude-haiku-4-5-20251001", 100_000, 0)
    with pytest.raises(BudgetExceeded, match="budget exceeded"):
        t.check_budget()


def test_check_budget_error_includes_numbers() -> None:
    t = CostTracker(budget_usd=0.01)
    t.record("A", "claude-haiku-4-5-20251001", 100_000, 0)
    with pytest.raises(BudgetExceeded) as exc_info:
        t.check_budget()
    msg = str(exc_info.value)
    assert "$0.08" in msg  # spent
    assert "$0.01" in msg  # cap


def test_check_budget_strict_inequality() -> None:
    """Exactly-at-cap should PASS (strict inequality) — the block
    triggers on the NEXT call, when spending has crossed the line."""
    t = CostTracker(budget_usd=1.0)
    t.record("x", "m", 1_000_000, 0, pricing=Pricing(1.0, 0.0))  # exactly $1
    # spent == budget, not strictly greater → no raise
    t.check_budget()


def test_unlimited_budget_never_raises() -> None:
    t = CostTracker(budget_usd=0.0)
    t.record("A", "claude-opus-4-7", 10_000_000, 10_000_000)  # ~$900
    t.check_budget()  # never raises for budget_usd <= 0


# ── remaining_usd ───────────────────────────────────────────────────────


def test_remaining_usd_positive_when_under_cap() -> None:
    t = CostTracker(budget_usd=1.0)
    t.record("A", "claude-haiku-4-5-20251001", 100_000, 0)  # $0.08
    assert t.remaining_usd == pytest.approx(0.92, abs=1e-6)


def test_remaining_usd_negative_when_over_cap() -> None:
    t = CostTracker(budget_usd=0.01)
    t.record("A", "claude-haiku-4-5-20251001", 100_000, 0)  # $0.08
    assert t.remaining_usd < 0


def test_remaining_usd_infinite_for_unlimited_budget() -> None:
    t = CostTracker(budget_usd=0.0)
    assert t.remaining_usd == float("inf")
    t.record("x", "m", 1000, 1000)
    assert t.remaining_usd == float("inf")
