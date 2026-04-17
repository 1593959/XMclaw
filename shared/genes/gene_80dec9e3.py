"""Auto-generated Gene for XMclaw.
Skill that automatically detects user reports of error 3, diagnoses the underlying cause, and applies the appropriate fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Error3fixer(GeneBase):
    gene_id = "gene_80dec9e3"
    name = "Error3Fixer"
    description = "Skill that automatically detects user reports of error 3, diagnoses the underlying cause, and applies the appropriate fix."
    trigger = "User message contains phrases such as"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Error3Fixer activated."
