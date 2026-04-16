"""
A skill that automatically fixes the reported error 1 based on the user's description.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorfixer(GeneBase):
    gene_id = "gene_70310d35"
    name = "ErrorFixer"
    description = """A skill that automatically fixes the reported error 1 based on the user's description."""
    trigger = "User says 'this is broken, please fix error 1' or reports a similar error code."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = 'error_1'
        error_details = context.get('error_details', {})
        component = error_details.get('component')
        if component == 'X':
            fix = apply_fix_for_x(error_details)
        else:
            fix = generic_fix(error_details)
        context['applied_fix'] = fix
        return {'status': 'fixed', 'fix': fix}
        return "Gene ErrorFixer activated."
