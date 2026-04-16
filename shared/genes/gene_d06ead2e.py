"""
Skill to handle user reports about error 3, diagnosing and fixing the issue
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3(GeneBase):
    gene_id = "gene_d06ead2e"
    name = "FixError3"
    description = """Skill to handle user reports about error 3, diagnosing and fixing the issue"""
    trigger = "User reports 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            error_info = get_error_details(error_id=3)
            logger.error('Error 3 reported: {error_info}')
            fix_result = apply_fix(error_id=3)
            if fix_result:
                logger.info('Fix applied successfully')
                return {'status': 'fixed'}
            else:
                logger.warning('Fix could not be applied')
                return {'status': 'failed'}
        except Exception as e:
            logger.exception('Unexpected error while fixing error 3')
            return {'status': 'error', 'message': str(e)}
        return "Gene FixError3 activated."
