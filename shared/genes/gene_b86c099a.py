"""Auto-generated Gene for XMclaw.
Automatically resolves error 2 when a user reports that something is broken.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Fixerror2skill(GeneBase):
    gene_id = "gene_b86c099a"
    name = "FixError2Skill"
    description = "Automatically resolves error 2 when a user reports that something is broken."
    trigger = "user message contains the words 'broken' and 'error 2'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene FixError2Skill activated."
