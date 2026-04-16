"""
Skill that automatically diagnoses and resolves error 4 when users report it as broken.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error4fixer(GeneBase):
    gene_id = "gene_db739ae1"
    name = "Error4Fixer"
    description = """Skill that automatically diagnoses and resolves error 4 when users report it as broken."""
    trigger = "User says 'this is broken, please fix error 4' or logs error 4 in the system."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_id = context.get('error_id', 4)
        logger.info(f"Attempting to fix error {error_id}")
        if error_id == 4:
            # Reset cache to clear corrupted state
            cache = self._get_cache()
            cache.clear()
            # Retry the failed operation
            result = self._retry_operation(context.get('operation'))
            if result.success:
                logger.info('Error 4 fixed successfully.')
                return {'status': 'resolved', 'message': 'Error 4 has been fixed.'}
            else:
                logger.error('Failed to resolve error 4.')
                return {'status': 'failed', 'message': 'Could not fix error 4.'}
        else:
            logger.warning(f"Unknown error id {error_id}")
            return {'status': 'skipped', 'message': 'Unknown error.'}
        return "Gene Error4Fixer activated."
