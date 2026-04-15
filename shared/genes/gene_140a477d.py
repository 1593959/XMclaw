"""
When a user reports that something is broken and specifically mentions error 3, the system automatically creates a high‑priority support ticket, assigns it to the appropriate support queue, and notifies the user of the ticket creation.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Auto‑createSupportTicketOnIssueReport(GeneBase):
    gene_id = "gene_140a477d"
    name = "Auto‑Create Support Ticket on Issue Report"
    description = """When a user reports that something is broken and specifically mentions error 3, the system automatically creates a high‑priority support ticket, assigns it to the appropriate support queue, and notifies the user of the ticket creation."""
    trigger = "{'type': 'user_message_match', 'conditions': {'contains_all': ['broken', 'error 3']}, 'case_insensitive': True}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "").lower()
        if "broken" in user_input and "error 3" in user_input:
            original_input = context.get("user_input", "")
            ticket = await self.create_support_ticket(
                title="User reported broken functionality – error 3",
                description=f'User reported: "{original_input}". Immediate investigation required.',
                priority="high",
                assignee="support_team",
                notification={"channel": "email", "template": "ticket_created_user"}
            )
            ticket_id = ticket.get("id", "N/A")
            return f"A high‑priority support ticket (ID: {ticket_id}) has been created and you will receive an email notification shortly."
        return ""
        return "Gene Auto‑Create Support Ticket on Issue Report activated."
