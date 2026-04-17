"""
Detects when a user reports an issue containing the phrase "broken still" and automatically creates a high-priority support ticket to ensure rapid resolution by the support team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BrokenStillIssueHandler(GeneBase):
    gene_id = "gene_43da95bd"
    name = "Broken Still Issue Handler"
    description = """Detects when a user reports an issue containing the phrase "broken still" and automatically creates a high-priority support ticket to ensure rapid resolution by the support team."""
    trigger = "{'type': 'UserReportedIssue', 'condition': {'issueText': {'operator': 'contains', 'value': 'broken still'}}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        if "broken still" in user_input.lower():
            message = f"User reported issue: {user_input}. Please investigate immediately."
            await self.create_support_ticket(
                priority="high",
                category="Bug",
                assign_to="support-team",
                notify_channels=["support", "engineering"],
                message_template=message
            )
            return "High-priority support ticket created."
        return "No 'broken still' issue detected."
        return "Gene Broken Still Issue Handler activated."