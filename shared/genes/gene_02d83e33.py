"""Auto-generated Gene for XMclaw.
Skill to handle user reports about error 4, retrieve a known fix, apply it, and confirm to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Error4fixskill(GeneBase):
    gene_id = "gene_02d83e33"
    name = "Error4FixSkill"
    description = "Skill to handle user reports about error 4, retrieve a known fix, apply it, and confirm to the user."
    trigger = "User says 'this is broken, please fix error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Error4FixSkill activated."
