"""Token and budget tracking."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CostTracker:
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        # Rough pricing estimates
        rates = {
            "gpt-4.1": {"prompt": 2.0, "completion": 8.0},
            "claude-sonnet-4-6": {"prompt": 3.0, "completion": 15.0},
        }
        rate = rates.get(model, {"prompt": 1.0, "completion": 4.0})
        cost = (prompt_tokens / 1_000_000 * rate["prompt"] +
                completion_tokens / 1_000_000 * rate["completion"])

        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_cost_usd += cost
        self.calls.append({
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost,
        })

    def summary(self) -> dict[str, Any]:
        return {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "call_count": len(self.calls),
        }
