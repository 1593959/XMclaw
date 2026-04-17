"""
Detects when a user reports a broken functionality referencing 'error 4' and automatically creates a high-priority support ticket to track and resolve the issue.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class HandleUserReportedError4(GeneBase):
    gene_id = "gene_c2032b84"
    name = "Handle User Reported Error 4"
    description = """Detects when a user reports a broken functionality referencing 'error 4' and automatically creates a high-priority support ticket to track and resolve the issue."""
    trigger = "{'type': 'feedback', 'pattern': 'this is broken, please fix error 4'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        if "this is broken, please fix error 4" in user_input.lower():
            ticket_details = {
                "subject": "User reported error 4",
                "priority": "high",
                "category": "Bug",
                "assignee": "support-team",
                "sendNotification": True
            }
            await self.create_ticket(ticket_details)
            return "High-priority support ticket created for error 4."
        return "No relevant error reported; no action taken."
        return "Gene Handle User Reported Error 4 activated."