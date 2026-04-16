"""
Skill that automatically addresses the user‑reported error 3 and attempts to resolve it.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3(GeneBase):
    gene_id = "gene_37a28913"
    name = "FixError3"
    description = """Skill that automatically addresses the user‑reported error 3 and attempts to resolve it."""
    trigger = "error 3"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            error_info = context.get('error_details', {})
            logger.error(f"Error 3 encountered: {error_info}")
            diagnosis = diagnose_error_3(error_info)
            fix_result = apply_fix_3(diagnosis)
            return {'status': 'fixed', 'details': fix_result}
        except Exception as e:
            logger.exception('Failed to fix error 3')
            return {'status': 'error', 'message': str(e)}
        return "Gene FixError3 activated."
