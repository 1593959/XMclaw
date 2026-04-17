"""Auto-generated Gene for XMclaw.
When a user reports a bug, automatically create a bug ticket, assign it to the relevant development team, and notify the user of the ticket and expected resolution timeline.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class UserReportedBugFix(GeneBase):
    gene_id = "gene_a60cd681"
    name = "User Reported Bug Fix"
    description = "When a user reports a bug, automatically create a bug ticket, assign it to the relevant development team, and notify the user of the ticket and expected resolution timeline."
    trigger = "User submits a bug report through the support portal, email, or in-app feedback channel."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene User Reported Bug Fix activated."
