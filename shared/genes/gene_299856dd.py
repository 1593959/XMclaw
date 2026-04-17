"""
Handles user reports of broken functionality when error code 0 is mentioned, logs the issue, runs diagnostics, and attempts to provide a fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorzerofix(GeneBase):
    gene_id = "gene_299856dd"
    name = "ErrorZeroFix"
    description = """Handles user reports of broken functionality when error code 0 is mentioned, logs the issue, runs diagnostics, and attempts to provide a fix."""
    trigger = "User says 'this is broken, please fix error 0' or a similar phrase containing 'error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_message = context.get('user_message', '')
        if 'error 0' in user_message.lower():
            logger.error('Error 0 reported by user')
            diagnostics_result = self.run_diagnostics(error_code=0)
            if diagnostics_result.get('fix_available'):
                fix = diagnostics_result['fix']
                return {'status': 'fixed', 'fix': fix}
            else:
                return {'status': 'error', 'message': 'No fix available for error 0'}
        else:
            return {'status': 'ignored', 'message': 'Trigger not matched'}
        return "Gene ErrorZeroFix activated."