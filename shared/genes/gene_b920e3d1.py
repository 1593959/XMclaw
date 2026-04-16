"""
Skill that captures user reports of broken functionality (e.g., "this is broken, please fix error 1") and automatically logs the issue, creates a high‑priority tracking ticket, and notifies the development team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Brokenissuehandler(GeneBase):
    gene_id = "gene_b920e3d1"
    name = "BrokenIssueHandler"
    description = """Skill that captures user reports of broken functionality (e.g., "this is broken, please fix error 1") and automatically logs the issue, creates a high‑priority tracking ticket, and notifies the development team."""
    trigger = "User message contains the word "broken" or mentions "error 1"."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        issue = context.user_message
        log_entry = f'User reported issue: {issue}'
        logger.error(log_entry)
        ticket = tracker.create_ticket(title='User reported broken', description=issue, priority='high')
        notify_team(f'New ticket created: {ticket.id}')
        return {'status': 'success', 'ticket_id': ticket.id}
        return "Gene BrokenIssueHandler activated."
