"""
Skill that handles user reports about broken functionality and attempts to resolve error 3.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_2ad6f72c"
    name = "FixError3Skill"
    description = """Skill that handles user reports about broken functionality and attempts to resolve error 3."""
    trigger = "User message contains 'broken' and 'error 3' or the phrase 'please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info('User reported issue: this is broken, please fix error 3')
        error_details = get_error_details(3)
        if error_details:
            apply_fix(error_details)
            user.reply('Error 3 has been fixed. Please try again.')
        else:
            user.reply('Unable to locate error 3. Please contact support.')
        return "Gene FixError3Skill activated."