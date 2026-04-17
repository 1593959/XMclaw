"""
Automatically resolves error 1 when a user reports that something is broken. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Erroronefixskill(GeneBase):
    gene_id = "gene_72c6770f"
    name = "ErrorOneFixSkill"
    description = """Automatically resolves error 1 when a user reports that something is broken."""
    trigger = "User input contains 'error 1', 'fix error 1', or 'broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Log the reported issue
        self.logger.info("User reported issue: " + context.get('message', ''))
        # Apply the fix for error 1
        self.fix_service.apply_fix('error_1')
        # Confirm resolution to user
        self.send_reply(context['user_id'], 'Error 1 has been fixed. Please try again.')
        return "Gene ErrorOneFixSkill activated."