"""
Skill to handle user reports of error 0 and attempt to fix it.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0(GeneBase):
    gene_id = "gene_99067677"
    name = "FixError0"
    description = """Skill to handle user reports of error 0 and attempt to fix it."""
    trigger = "User says 'this is broken, please fix error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error('Error 0 reported')
        context = self.get_context()
        fix_applied = self.apply_fix(context, error_code=0)
        if fix_applied:
            return {'status': 'fixed', 'message': 'Error 0 has been resolved.'}
        else:
            return {'status': 'failed', 'message': 'Unable to fix error 0.'}
        return "Gene FixError0 activated."