"""
Automatically processes user-reported bug issues by creating a bug ticket, assigning the development team, notifying the user, and setting the ticket status to Open.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Bugreporthandler(GeneBase):
    gene_id = "gene_dcc67cc1"
    name = "BugReportHandler"
    description = """Automatically processes user-reported bug issues by creating a bug ticket, assigning the development team, notifying the user, and setting the ticket status to Open."""
    trigger = "{'type': 'event', 'event': 'user.reported_issue', 'filters': {'issue_type': 'bug'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        issue_type = context.get("issue_type", "")
        if issue_type != "bug":
            return "Issue is not a bug, ignoring."
        
        user_input = context.get("user_input", "")
        user_id = context.get("user_id", "")
        user_email = context.get("user_email", "")
        
        # Create a bug ticket in the issue tracking system
        ticket_id = await create_ticket(
            type="bug",
            status="Open",
            description=user_input,
            reporter=user_id
        )
        
        # Assign the ticket to the development team
        await assign_team(
            ticket_id=ticket_id,
            team="Development"
        )
        
        # Send a confirmation email to the user
        await notify_user(
            user_email=user_email,
            template="bug_reported_confirmation",
            context={"ticket_id": ticket_id}
        )
        
        return f"Bug ticket {ticket_id} created, assigned to Development, and confirmation sent to {user_email}."
        return "Gene BugReportHandler activated."