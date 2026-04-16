"""
Skill to diagnose and fix error 0 when user reports it broken.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0(GeneBase):
    gene_id = "gene_85f27ff5"
    name = "FixError0"
    description = """Skill to diagnose and fix error 0 when user reports it broken."""
    trigger = "User says: "this is broken, please fix error 0""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info('Received request to fix error 0')
        error_details = get_error_details('0')
        if error_details:
            fix_result = apply_fix(error_details)
            if fix_result:
                logger.info('Error 0 fixed successfully')
                return {'status': 'fixed', 'message': fix_result}
            else:
                logger.warning('Automatic fix for error 0 failed')
                return {'status': 'failed', 'message': 'Could not auto-fix error 0'}
        else:
            logger.warning('No details found for error 0')
            return {'status': 'unknown', 'message': 'Error 0 details not available'}
        return "Gene FixError0 activated."
