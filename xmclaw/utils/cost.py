"""Cost tracker with hard budget circuit-breaker — anti-req #6.

Runaway cost is unacceptable: the OpenClaw issue log alone shows users
burning $200 in a day to an infinite LLM loop. v2's posture is that
any session has a HARD cap; crossing it raises ``BudgetExceeded``
which the orchestrator catches and turns into a clean abort + an
``ANTI_REQ_VIOLATION`` event (no silent drop, no partial-success
pretense).

Two contracts:

  * ``record(provider, model, prompt_tokens, completion_tokens,
    pricing=None)``
      Record one LLM call's usage. Cost is derived from either the
      explicit ``pricing`` argument or ``DEFAULT_PRICING`` for the
      model. Unknown models cost 0 by default — i.e. "I don't know
      what this costs, don't block on it"; callers who care pass
      ``pricing`` explicitly.

  * ``check_budget()``
      Compare ``spent_usd`` against ``budget_usd``. Raises
      ``BudgetExceeded`` when over. Safe to call between calls or
      before the NEXT call is made — the typical pattern is
      "check → call → record", so we block before the next turn
      when we've already exceeded.

The implementation is intentionally standalone so it stays callable
from any layer (``core``, ``providers``, ``daemon``) without pulling
sibling subpackage imports through ``utils``.
"""
from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(Exception):
    """Raised when cumulative spend crosses the hard cap."""


@dataclass(frozen=True, slots=True)
class Pricing:
    """USD per million tokens, split by direction."""

    input_per_mtok: float
    output_per_mtok: float


# Per-provider / per-model pricing — exact match table.
# The defaults are the published list prices as of 2026-04 for the
# models most likely to be hit by ``xmclaw v2 chat`` out of the box.
# Use :func:`lookup_pricing` to get a Pricing for a model name with
# substring fallback for unknown-but-recognised families.
DEFAULT_PRICING: dict[str, Pricing] = {
    # Anthropic
    "claude-opus-4-7":            Pricing(15.0, 75.0),
    "claude-sonnet-4-6":          Pricing(3.0,  15.0),
    "claude-haiku-4-5-20251001":  Pricing(0.8,  4.0),
    # OpenAI
    "gpt-4o":      Pricing(2.5, 10.0),
    "gpt-4o-mini": Pricing(0.15, 0.6),
    "gpt-4.1":     Pricing(2.5, 10.0),
}


# B-335 (audit #17): substring-match patterns. Single source of
# truth — the analytics router used to ship its OWN parallel table
# with divergent values (claude-haiku 0.25/1.25 vs cost.py 0.8/4.0).
# Order matters — first match wins; specific before generic.
MODEL_PRICING_PATTERNS: tuple[tuple[str, Pricing], ...] = (
    ("gpt-4o-mini",    Pricing(0.15, 0.6)),
    ("gpt-4o",         Pricing(2.5,  10.0)),
    ("gpt-4.1",        Pricing(2.5,  10.0)),
    ("gpt-4",          Pricing(30.0, 60.0)),
    ("gpt-3.5",        Pricing(0.5,  1.5)),
    ("o1-",            Pricing(15.0, 60.0)),
    ("o3-",            Pricing(15.0, 60.0)),
    ("claude-haiku-4", Pricing(0.8,  4.0)),
    ("claude-3-haiku", Pricing(0.25, 1.25)),
    ("claude-haiku",   Pricing(0.8,  4.0)),
    ("claude-3-sonnet",Pricing(3.0,  15.0)),
    ("claude-sonnet",  Pricing(3.0,  15.0)),
    ("claude-3-opus",  Pricing(15.0, 75.0)),
    ("claude-opus",    Pricing(15.0, 75.0)),
    ("claude",         Pricing(3.0,  15.0)),
    ("gemini-1.5-pro", Pricing(1.25, 5.0)),
    ("gemini-pro",     Pricing(0.5,  1.5)),
    ("kimi",           Pricing(0.3,  1.2)),
    ("moonshot",       Pricing(0.3,  1.2)),
    ("qwen",           Pricing(0.3,  1.2)),
    ("glm",            Pricing(0.3,  1.2)),
    ("minimax",        Pricing(0.2,  0.8)),
    ("deepseek",       Pricing(0.14, 0.28)),
    ("llama",          Pricing(0.2,  0.6)),
)


# Conservative fallback when no pattern matches — better than 0
# (silently hides cost) and better than raising (analytics
# renders this every page-load).
DEFAULT_FALLBACK_PRICING: Pricing = Pricing(0.5, 1.5)


def lookup_pricing(model: str) -> Pricing:
    """B-335: single source of truth for model-name → Pricing.

    Resolution order:
      1. Exact match in DEFAULT_PRICING.
      2. First substring match in MODEL_PRICING_PATTERNS.
      3. DEFAULT_FALLBACK_PRICING.
    """
    if not model:
        return DEFAULT_FALLBACK_PRICING
    direct = DEFAULT_PRICING.get(model)
    if direct is not None:
        return direct
    name = model.lower()
    for substr, pricing in MODEL_PRICING_PATTERNS:
        if substr in name:
            return pricing
    return DEFAULT_FALLBACK_PRICING


@dataclass
class LedgerEntry:
    """One recorded call, kept for audit / debugging."""

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class CostTracker:
    """Accumulating cost ledger with a hard cap.

    Parameters
    ----------
    budget_usd : float
        Hard cap. Zero or negative means "unlimited" — a tracker
        constructed without a cap still records usage but never raises.
    pricing_overrides : dict | None
        Additional or replacement per-model pricing. Looked up BEFORE
        ``DEFAULT_PRICING``; models absent from both tables cost 0
        unless the caller passes explicit pricing to ``record``.
    """

    def __init__(
        self,
        budget_usd: float = 5.0,
        *,
        pricing_overrides: dict[str, Pricing] | None = None,
    ) -> None:
        self.budget_usd = budget_usd
        self.spent_usd = 0.0
        self._ledger: list[LedgerEntry] = []
        self._pricing: dict[str, Pricing] = {
            **DEFAULT_PRICING,
            **(pricing_overrides or {}),
        }

    # ── recording ──

    def record(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        pricing: Pricing | None = None,
    ) -> float:
        """Record one call's usage; return the cost attributed to it."""
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError(
                f"negative token counts: prompt={prompt_tokens}, "
                f"completion={completion_tokens}"
            )
        # B-340 (audit pass-2 #3): pre-fix this used raw dict.get
        # against the exact-match ``self._pricing`` dict. Models with
        # date-suffixed names (e.g. ``claude-haiku-4-5-20251002``)
        # or any non-default-named model (kimi / glm / minimax /
        # deepseek / qwen / etc) returned None → cost stayed 0 →
        # ``check_budget`` never tripped. Anti-req #6 (hard cost cap)
        # was bypassed for ~every real Chinese-market model. Now
        # falls back through ``lookup_pricing`` (substring + default)
        # which is the same source-of-truth the analytics dashboard
        # uses post-B-335; the two views can no longer disagree.
        if pricing is not None:
            effective: Pricing | None = pricing
        else:
            effective = self._pricing.get(model)
            if effective is None:
                effective = lookup_pricing(model)
        if effective is None:
            cost = 0.0
        else:
            cost = (
                prompt_tokens * effective.input_per_mtok / 1_000_000
                + completion_tokens * effective.output_per_mtok / 1_000_000
            )
        self.spent_usd += cost
        self._ledger.append(LedgerEntry(
            provider=provider, model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        ))
        return cost

    # ── gate ──

    def check_budget(self) -> None:
        """Raise ``BudgetExceeded`` when ``spent_usd > budget_usd``.

        Unlimited (``budget_usd <= 0``) never raises. Strict inequality
        so a caller who just hit the cap exactly can still finish the
        current call — the block triggers on the NEXT call, when
        ``spent_usd`` is already over.
        """
        if self.budget_usd > 0 and self.spent_usd > self.budget_usd:
            raise BudgetExceeded(
                f"budget exceeded: spent ${self.spent_usd:.4f} "
                f"> cap ${self.budget_usd:.4f}"
            )

    # ── audit ──

    @property
    def remaining_usd(self) -> float:
        """How much of the budget is left. Negative when over cap.

        ``float('inf')`` for unlimited trackers so consumers can
        compare against other numbers without special-casing.
        """
        if self.budget_usd <= 0:
            return float("inf")
        return self.budget_usd - self.spent_usd

    @property
    def ledger(self) -> list[LedgerEntry]:
        return list(self._ledger)
