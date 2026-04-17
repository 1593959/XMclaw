"""
Skill that automatically resolves user-reported error 3 by performing diagnostic checks and applying known fixes.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_0b15ec75"
    name = "FixError3Skill"
    description = """Skill that automatically resolves user-reported error 3 by performing diagnostic checks and applying known fixes."""
    trigger = "User reports 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = context.get('error_code')
        if error_code == 3:
            log_info('Fixing error 3...')
            result = fix_service.apply_fix('error_3')
            if result.success:
                log_info('Error 3 resolved successfully.')
                return {'status': 'resolved', 'message': 'Error 3 fixed.'}
            else:
                log_error('Failed to fix error 3: ' + result.message)
                return {'status': 'failed', 'message': result.message}
        else:
            log_warning('Received request for non-error-3, ignoring.')
            return {'status': 'ignored', 'message': 'Not error 3.'}
        return "Gene FixError3Skill activated."