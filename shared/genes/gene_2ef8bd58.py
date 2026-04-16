"""
Automatically resolves error 2 when a user reports a broken state and requests a fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2gene(GeneBase):
    gene_id = "gene_2ef8bd58"
    name = "FixError2Gene"
    description = """Automatically resolves error 2 when a user reports a broken state and requests a fix."""
    trigger = "User message contains 'error 2' together with a request to fix (e.g., 'this is broken, please fix error 2')."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            # Identify the component that raises error 2
            error_component = identify_component('error_2')
            # Apply the known fix for this error
            fix_result = error_component.apply_fix()
            logger.info('Error 2 resolved successfully')
            return {'status': 'fixed', 'details': fix_result}
        except Exception as e:
            logger.error('Failed to fix error 2', exc_info=True)
            raise
        return "Gene FixError2Gene activated."
