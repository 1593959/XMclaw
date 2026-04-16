"""
Skill that automatically detects and fixes error 1 when a user reports a broken state.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror1skill(GeneBase):
    gene_id = "gene_a7999a10"
    name = "FixError1Skill"
    description = """Skill that automatically detects and fixes error 1 when a user reports a broken state."""
    trigger = "User input containing 'broken' or 'error 1'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract error details from context
        error_info = self.context.get('error_info', {})
        if error_info.get('code') == 1:
            # Log fix attempt
            self.logger.info('Applying fix for error 1')
            # Perform specific remediation steps for error 1
            self.remediate()
        else:
            self.logger.warning('Error code does not match error 1')
        return "Gene FixError1Skill activated."
