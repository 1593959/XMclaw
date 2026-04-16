"""
Detects when a user reports a broken experience and mentions error 0, and provides diagnostic steps and possible fixes.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorzerofixer(GeneBase):
    gene_id = "gene_6c8d91e3"
    name = "ErrorZeroFixer"
    description = """Detects when a user reports a broken experience and mentions error 0, and provides diagnostic steps and possible fixes."""
    trigger = "User input contains the words 'broken' and 'error 0' (case‑insensitive)"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_msg = user_input.get('message', '')
        if 'broken' in error_msg.lower() and 'error 0' in error_msg.lower():
            steps = ['Check the system status.', 'Verify the configuration file.', 'Restart the service.', 'If the problem persists, contact support.']
            return {'response': f'I’m sorry you’re experiencing a broken state with error 0. Here are some steps to resolve it: {chr(10).join(steps)}'}
        else:
            return {'response': 'This skill is triggered only when you mention broken and error 0.'}
        return "Gene ErrorZeroFixer activated."
