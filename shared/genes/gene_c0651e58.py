"""
Skill that detects when a user reports error 0 or says the system is broken and attempts to fix the error automatically.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0skill(GeneBase):
    gene_id = "gene_c0651e58"
    name = "FixError0Skill"
    description = """Skill that detects when a user reports error 0 or says the system is broken and attempts to fix the error automatically."""
    trigger = "User input matches patterns like 'error 0', 'broken', or 'fix error' (case-insensitive)."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get('user_input', '')
        if 'error 0' in user_input.lower() or 'broken' in user_input.lower():
            self.logger.info('Error 0 reported. Starting diagnostic...')
            try:
                self.reset_component('target')
                self.retry_operation('target')
                self.logger.info('Error 0 resolved.')
                context['response'] = 'Error 0 has been fixed. Please try again.'
            except Exception as e:
                self.logger.error(f'Failed to fix error 0: {e}')
                context['response'] = 'Unable to fix error 0 automatically. Please contact support.'
        else:
            context['response'] = 'No relevant error detected.'
        return "Gene FixError0Skill activated."