"""Auto-generated Gene for XMclaw.
Skill that detects when a user reports a broken state with error 4, attempts to diagnose the issue using the knowledge base, and returns a fix or guidance.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Fixerror4(GeneBase):
    gene_id = "gene_ea26e120"
    name = "FixError4"
    description = "Skill that detects when a user reports a broken state with error 4, attempts to diagnose the issue using the knowledge base, and returns a fix or guidance."
    trigger = "User says something like"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene FixError4 activated."
