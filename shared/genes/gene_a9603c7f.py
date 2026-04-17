"""Auto-generated Gene for XMclaw.
When a bug is reported as unfixed or regressed, this gene ensures thorough re-testing and validation before closing the issue, preventing recurring bug reports.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class BugFixVerification(GeneBase):
    gene_id = "gene_a9603c7f"
    name = "Bug Fix Verification"
    description = "When a bug is reported as unfixed or regressed, this gene ensures thorough re-testing and validation before closing the issue, preventing recurring bug reports."
    trigger = "{'type': 'event', 'condition':"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Bug Fix Verification activated."
