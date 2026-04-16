"""
Skill that automatically attempts to fix error 2 when a user reports a broken state and asks for a fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_5954a043"
    name = "FixError2Skill"
    description = """Skill that automatically attempts to fix error 2 when a user reports a broken state and asks for a fix."""
    trigger = "User input contains the words 'broken' and 'error 2' (case‑insensitive)."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            error_id = 2
            logger.info('Attempting to fix error ' + str(error_id))
            fix_result = error_fixer.apply_fix(error_id)
            context['fix_result'] = fix_result
            return fix_result
        except Exception as e:
            logger.error('Failed to fix error ' + str(error_id) + ': ' + str(e))
            context['error'] = str(e)
            raise
        return "Gene FixError2Skill activated."
