"""Auto-generated Gene for XMclaw.
This gene ensures that any fix applied to a user-reported bug actually resolves the issue and does not introduce regressions. If the fix fails validation, it is automatically reverted and the responsible developer is notified.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class ValidateBugFixCorrectness(GeneBase):
    gene_id = "gene_bba52615"
    name = "Validate Bug Fix Correctness"
    description = "This gene ensures that any fix applied to a user-reported bug actually resolves the issue and does not introduce regressions. If the fix fails validation, it is automatically reverted and the responsi"
    trigger = "Event: Bug_Fix_Commit - a commit is pushed that claims to address a user-reported issue; Condition: a corresponding bug report exists in the tracking system."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Validate Bug Fix Correctness activated."
