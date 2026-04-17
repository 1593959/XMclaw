"""
Skill to handle user reports of error 0, logging the issue, providing troubleshooting steps, and notifying support.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorzeroskill(GeneBase):
    gene_id = "gene_e6b1e977"
    name = "FixErrorZeroSkill"
    description = """Skill to handle user reports of error 0, logging the issue, providing troubleshooting steps, and notifying support."""
    trigger = "User message contains error 0 or broken"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_message = context.get('message')
        if 'error 0' in user_message.lower():
            logger.error('User reported error 0: %s', user_message)
            response = 'It looks like you are encountering error 0. Please try the following steps: ...'
            context['response'] = response
            notify_support()
        return "Gene FixErrorZeroSkill activated."
