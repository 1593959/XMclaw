"""
Detects user messages reporting a broken feature with error 4, acknowledges the issue, provides known troubleshooting steps, and logs a support ticket. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error4fixresponder(GeneBase):
    gene_id = "gene_46cde8db"
    name = "Error4FixResponder"
    description = """Detects user messages reporting a broken feature with error 4, acknowledges the issue, provides known troubleshooting steps, and logs a support ticket."""
    trigger = "{'type': 'message', 'match': {'contains': ['broken', 'error 4']}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_msg = context.user_message
        logger.info(f'User reported issue: {user_msg}')
        response = (
    'We apologize for the inconvenience. To resolve error 4, please try the following steps:\n'
    '1. Clear your browser cache and cookies.\n'
    '2. Ensure your app is updated to the latest version.\n'
    '3. Restart the application.\n'
    'If the issue persists, please let us know and we will investigate further.'
        )
        context.reply(response)
        ticket_id = tickets.create(
    title=f'Error 4 reported: {user_msg}',
    priority='high',
    tags=['error-4', 'user-report']
        )
        logger.info(f'Created support ticket {ticket_id}')
        return "Gene Error4FixResponder activated."