"""Cost tracker with hard budget circuit-breaker.

Anti-req #6: runaway cost is unacceptable. ``BudgetExceeded`` is raised when
the session cap is hit; scheduler catches it and aborts the current run.

Phase 1: stub. Will re-export or wrap ``xmclaw.core.cost_tracker`` (v1)
until v1 module is retired.
"""
from __future__ import annotations


class BudgetExceeded(Exception):
    """Raised when cumulative spend crosses the hard cap."""


class CostTracker:
    def __init__(self, budget_usd: float = 5.0) -> None:
        self.budget_usd = budget_usd
        self.spent_usd = 0.0

    def record(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:  # noqa: ARG002
        raise NotImplementedError("Phase 1 — port from xmclaw.core.cost_tracker with budget check")

    def check_budget(self) -> None:
        if self.spent_usd > self.budget_usd:
            raise BudgetExceeded(f"spent ${self.spent_usd:.2f} > cap ${self.budget_usd:.2f}")
