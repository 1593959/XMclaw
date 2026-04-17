"""Auto-generated Gene for XMclaw.
Detects when a user reports a broken functionality that mentions 'error 2' and automatically creates a support ticket, notifies the support team, and displays an acknowledgment to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class UserReportedIssueHandler(GeneBase):
    gene_id = "gene_63f4eb7b"
    name = "User Reported Issue Handler"
    description = "Detects when a user reports a broken functionality that mentions 'error 2' and automatically creates a support ticket, notifies the support team, and displays an acknowledgment to the user."
    trigger = "{'type': 'user_message', 'conditions': {'contains': ['broken', 'error 2']}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene User Reported Issue Handler activated."
