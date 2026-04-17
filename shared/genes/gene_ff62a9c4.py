"""
Skill that captures user-reported bugs (e.g., "this is broken, please fix error 1") and automatically creates a bug ticket while alerting the development team. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Bugreporthandler(GeneBase):
    gene_id = "gene_ff62a9c4"
    name = "BugReportHandler"
    description = """Skill that captures user-reported bugs (e.g., "this is broken, please fix error 1") and automatically creates a bug ticket while alerting the development team."""
    trigger = "User says \"this is broken\" or \"please fix error 1\""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Log the reported issue
        logger.error('User reported issue: this is broken, please fix error 1')
        # Create a bug ticket in the ticketing system
        ticket = bug_tracker.create_ticket(
    title='Bug: Error 1 reported by user',
    description='User says: this is broken, please fix error 1',
    severity='high'
        )
        # Notify the development team
        notification_service.send_alert(
    team='development',
    message=f'New bug ticket created: {ticket.id}'
        )
        return {'status': 'success', 'ticket_id': ticket.id}
        return "Gene BugReportHandler activated."