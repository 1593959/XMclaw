"""
Skill that detects a user complaint about 'this is broken, please fix error 0' and attempts to resolve the issue by resetting the component that generated error 0.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0(GeneBase):
    gene_id = "gene_b67e3dfc"
    name = "FixError0"
    description = """Skill that detects a user complaint about 'this is broken, please fix error 0' and attempts to resolve the issue by resetting the component that generated error 0."""
    trigger = "this is broken, please fix error 0"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error('Error 0 reported: resetting component...')
        # Identify the component that raised error 0
        component = context.get('component')
        if component:
            component.reset()
            logger.info('Component reset successfully.')
            return {'status': 'resolved', 'message': 'Error 0 has been fixed.'}
        else:
            logger.warning('No component found for error 0.')
            return {'status': 'failed', 'message': 'Unable to locate component.'}
        return "Gene FixError0 activated."