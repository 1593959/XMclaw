"""
Skill that detects when a user reports 'this is broken, please fix error 3', logs the issue, attempts to resolve error 3, and replies to the user with the outcome.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_9213730a"
    name = "FixError3Skill"
    description = """Skill that detects when a user reports 'this is broken, please fix error 3', logs the issue, attempts to resolve error 3, and replies to the user with the outcome."""
    trigger = "User says 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Parse the incoming user message
        message = context.get('message', '')
        if 'error 3' in message.lower():
            logger.info('User reported error 3')
            # Attempt to resolve error 3
            try:
                fix_result = fix_error_3()
                reply = f'Error 3 has been fixed. Details: {fix_result}'
            except Exception as e:
                logger.error('Failed to fix error 3', exc_info=True)
                reply = 'Sorry, we could not fix error 3 at this time.'
            return reply
        return None
        return "Gene FixError3Skill activated."