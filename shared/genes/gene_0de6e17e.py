"""
Skill to automatically handle user reports of 'error 2' by logging the issue, retrieving known fix steps, and presenting the solution to the user. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_0de6e17e"
    name = "FixError2Skill"
    description = """Skill to automatically handle user reports of 'error 2' by logging the issue, retrieving known fix steps, and presenting the solution to the user."""
    trigger = "User input contains phrases such as 'broken', 'fix error 2', or 'error 2'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        self.logger.error('User reported error 2: ' + self.user_message)
        self.context['error_type'] = 'error_2'
        fix_steps = self.fetch_fix_steps('error_2')
        self.ui.show_message('I detected error 2. Here is a possible fix: ' + fix_steps)
        return "Gene FixError2Skill activated."