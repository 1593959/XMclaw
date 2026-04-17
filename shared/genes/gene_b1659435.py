"""Auto-generated Gene for XMclaw.
Skill that reacts when a user reports a broken functionality and attempts to resolve error 3.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Fixerror3skill(GeneBase):
    gene_id = "gene_b1659435"
    name = "FixError3Skill"
    description = "Skill that reacts when a user reports a broken functionality and attempts to resolve error 3."
    trigger = "{'type': 'regex', 'pattern': 'error\\\\s*3|broken'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene FixError3Skill activated."
