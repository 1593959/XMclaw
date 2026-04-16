"""
Automatically handles user reports of a broken component with error 2 by applying the predefined fix and returning the outcome.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2gene(GeneBase):
    gene_id = "gene_c44ce080"
    name = "FixError2Gene"
    description = """Automatically handles user reports of a broken component with error 2 by applying the predefined fix and returning the outcome."""
    trigger = "User message matches pattern "this is broken, please fix error 2""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            error_id = context.get('error_id', 'unknown')
            fix_result = services.error_fix_service.apply_fix(error_id, target='error_2')
            if fix_result.success:
                return {'status': 'fixed', 'message': 'Error 2 has been resolved.'}
            else:
                return {'status': 'failed', 'message': fix_result.reason}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}
        return "Gene FixError2Gene activated."
