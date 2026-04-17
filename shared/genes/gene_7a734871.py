"""Auto-generated Gene for XMclaw.
Automatically handles cases where a user reports a bug that has already been fixed, ensuring a regression workflow is triggered to verify the fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class BugRegressionHandler(GeneBase):
    gene_id = "gene_7a734871"
    name = "Bug Regression Handler"
    description = "Automatically handles cases where a user reports a bug that has already been fixed, ensuring a regression workflow is triggered to verify the fix."
    trigger = "{'type': 'BugReopened', 'condition':"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Bug Regression Handler activated."
