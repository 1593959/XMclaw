"""
Handles user reports of 'this is broken, please fix error 4' by logging the issue, retrieving known fixes, and applying a solution.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_ce86aed4"
    name = "FixError4Skill"
    description = """Handles user reports of 'this is broken, please fix error 4' by logging the issue, retrieving known fixes, and applying a solution."""
    trigger = "User input contains 'error 4' and a request to fix, e.g., 'this is broken, please fix error 4'."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        log_error('User reported broken: error 4')
        fix = get_known_fix('error_4')
        if fix:
            user.reply(f'Found fix for error 4: {fix}')
            apply_fix(fix)
        else:
            user.reply('Sorry, I could not find a known fix for error 4. Please provide more details.')
        return "Gene FixError4Skill activated."
