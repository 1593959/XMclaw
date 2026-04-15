"""
When a user reports a bug (e.g., via the support portal), automatically create a bug ticket, assign it to the development team with status 'Open' and priority 'Medium', and send an acknowledgement email to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixbugonuserreport(GeneBase):
    gene_id = "gene_25a5602f"
    name = "FixBugOnUserReport"
    description = """When a user reports a bug (e.g., via the support portal), automatically create a bug ticket, assign it to the development team with status 'Open' and priority 'Medium', and send an acknowledgement email to the user."""
    trigger = "{'type': 'user_report', 'event': 'bug_report', 'source': 'support_portal'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            user_email = context.get("user_email") or context.get("user", {}).get("email", "unknown@example.com")
            bug_description = user_input
        
            ticket = await create_ticket(
                ticket_type="bug",
                status="Open",
                priority="Medium",
                description=bug_description,
                reporter_email=user_email
            )
            ticket_id = ticket.get("id")
        
            await assign_ticket(
                ticket_id=ticket_id,
                team="development",
                role="developer"
            )
        
            await notify_user(
                channel="email",
                template="bug_acknowledgement",
                recipient=user_email,
                ticket_id=ticket_id
            )
        
            return f"Bug report received. Ticket {ticket_id} created, assigned to the development team, and an acknowledgement email sent to {user_email}."
        return "Gene FixBugOnUserReport activated."
