"""
Handles user reports of a broken system with error 0 by logging the issue and providing troubleshooting steps.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0skill(GeneBase):
    gene_id = "gene_3c5bd4ba"
    name = "FixError0Skill"
    description = """Handles user reports of a broken system with error 0 by logging the issue and providing troubleshooting steps."""
    trigger = "User input contains 'this is broken, please fix error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error('User reported error 0: broken')
        response = 'Sorry you are experiencing error 0. Please try restarting the service or contacting support.'
        return response
        return "Gene FixError0Skill activated."
