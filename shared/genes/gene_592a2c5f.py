"""
A skill that detects when a user reports that something is broken or mentions error 4 and provides a known fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_592a2c5f"
    name = "FixError4Skill"
    description = """A skill that detects when a user reports that something is broken or mentions error 4 and provides a known fix."""
    trigger = "User message containing the words 'broken' or 'error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_message = context.get('user_message', '')
        if 'error 4' in user_message.lower() or 'broken' in user_message.lower():
            logging.warning(f'User reported issue: {user_message}')
            fix_steps = self.get_fix_steps('error_4')
            response = 'It looks like you are encountering error 4. Here are suggested steps:\n{}'.format(fix_steps)
            context['response'] = response
        else:
            context['response'] = 'I am not sure what you need. Could you provide more details?'
        return "Gene FixError4Skill activated."
