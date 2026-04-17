"""
A skill that automatically resolves error code 3 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3(GeneBase):
    gene_id = "gene_9a9bc153"
    name = "FixError3"
    description = """A skill that automatically resolves error code 3 reported by users."""
    trigger = "User says 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve current error details
        error_info = context.get('error_info')
        if error_info and error_info.get('code') == 3:
            # Apply the correction patch
            fix_result = system.apply_patch(error_info)
            context['response'] = f'Error 3 has been resolved. Details: {fix_result}'
        else:
            context['response'] = 'Error 3 not found or already fixed.'
        return "Gene FixError3 activated."