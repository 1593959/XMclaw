"""Auto-generated Gene for XMclaw.
This gene activates when a user explicitly requests that a bug be fixed again. It triggers a fresh bug-fix workflow that re-creates or re-opens a bug ticket, notifies the responsible developer, re-runs the related CI tests, and logs the retry attempt.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class RetryBugFix(GeneBase):
    gene_id = "gene_2482ac02"
    name = "Retry Bug Fix"
    description = "This gene activates when a user explicitly requests that a bug be fixed again. It triggers a fresh bug-fix workflow that re-creates or re-opens a bug ticket, notifies the responsible developer, re-run"
    trigger = "{'type': 'user_intent', 'condition': 'user input matches the pattern"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Retry Bug Fix activated."
