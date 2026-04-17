"""
Skill that automatically resolves error code 0 reported by users, performing a service reset and cache clear.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0skill(GeneBase):
    gene_id = "gene_8e347fbf"
    name = "FixError0Skill"
    description = """Skill that automatically resolves error code 0 reported by users, performing a service reset and cache clear."""
    trigger = "User says 'this is broken, please fix error 0' or error code 0 is detected."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger = context.logger
        error_code = context.error_code
        if error_code == 0:
            logger.info('Detected error 0, initiating fix...')
            self._reset_service()
            self._clear_cache()
            logger.info('Fix completed successfully.')
        else:
            logger.warning('Error code not handled: %s', error_code)
        return "Gene FixError0Skill activated."