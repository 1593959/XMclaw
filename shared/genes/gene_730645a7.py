"""
Skill that intercepts user reports of 'this is broken, please fix error 4' and attempts to resolve error 4 in the system.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_730645a7"
    name = "FixError4Skill"
    description = """Skill that intercepts user reports of 'this is broken, please fix error 4' and attempts to resolve error 4 in the system."""
    trigger = "User says 'this is broken, please fix error 4' or similar phrasing indicating error 4"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logging.error('Error 4 reported by user')
        # Gather context about the error
        context = get_context()
        # Determine if this is error 4
        if context.get('error_code') == 4:
            # Perform fix actions
            component = get_component(context)
            component.reset()
            return {'status': 'fixed', 'message': 'Error 4 has been resolved.'}
        else:
            # If not error 4, return unresolved
            return {'status': 'unresolved', 'message': 'Unable to fix error 4.'}
        return "Gene FixError4Skill activated."
