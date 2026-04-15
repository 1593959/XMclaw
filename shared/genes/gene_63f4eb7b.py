"""
Detects when a user reports a broken functionality that mentions 'error 2' and automatically creates a support ticket, notifies the support team, and displays an acknowledgment to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class UserReportedIssueHandler(GeneBase):
    gene_id = "gene_63f4eb7b"
    name = "User Reported Issue Handler"
    description = """Detects when a user reports a broken functionality that mentions 'error 2' and automatically creates a support ticket, notifies the support team, and displays an acknowledgment to the user."""
    trigger = "{'type': 'user_message', 'conditions': {'contains': ['broken', 'error 2']}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            if "broken" in user_input.lower() and "error 2" in user_input.lower():
                ticket = await self.create_ticket(
                    title="User reported issue: broken, please fix error 2",
                    priority="high",
                    tags=["user-reported", "error-2"]
                )
                await self.send_notification(
                    team="support",
                    message="User reported a broken issue with error 2"
                )
                ack_msg = await self.display_message(
                    message="We apologize for the inconvenience. Our team has been notified and will address the issue shortly."
                )
                return f"Issue detected. Support ticket {ticket.get('id', 'N/A')} created, team notified, and user acknowledged."
            return "No relevant issue detected."
        return "Gene User Reported Issue Handler activated."
