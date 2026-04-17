"""Auto-generated Gene for XMclaw.
Skill to automatically fix error 1 when the user reports it.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Error1fixskill(GeneBase):
    gene_id = "gene_0f6e82b9"
    name = "Error1FixSkill"
    description = "Skill to automatically fix error 1 when the user reports it."
    trigger = "User says"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Error1FixSkill activated."
