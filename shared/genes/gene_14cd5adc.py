"""
Skill to resolve the reported error 3 ('this is broken, please fix error 3').
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_14cd5adc"
    name = "FixError3Skill"
    description = """Skill to resolve the reported error 3 ('this is broken, please fix error 3')."""
    trigger = "User reports 'this is broken, please fix error 3'."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            error_info = self.get_error_info(3)
            self.logger.info(f'Reported error 3: {error_info}')
            self.fix_error(error_info)
            return {'status': 'fixed', 'error_id': 3}
        except Exception as e:
            self.logger.error(f'Failed to fix error 3: {e}')
            return {'status': 'failed', 'error_id': 3, 'message': str(e)}
        return "Gene FixError3Skill activated."
