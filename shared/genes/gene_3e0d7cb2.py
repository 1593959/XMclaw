"""
Skill to automatically address user‑reported ‘error 1’, performing diagnosis and corrective actions.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror1skill(GeneBase):
    gene_id = "gene_3e0d7cb2"
    name = "FixError1Skill"
    description = """Skill to automatically address user‑reported ‘error 1’, performing diagnosis and corrective actions."""
    trigger = "User input containing ‘this is broken, please fix error 1’"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Diagnose the error
        error_details = context.get('error_details', {})
        logger.error(f'Error 1 reported: {error_details}')
        # Perform corrective steps
        try:
            component = error_details.get('component')
            if component:
                component.reset()
                logger.info(f'Successfully reset component {component}')
                return {'status': 'success', 'message': 'Error 1 fixed.'}
        except Exception as e:
            logger.exception(f'Failed to fix error 1: {e}')
            return {'status': 'failure', 'message': str(e)}
        return "Gene FixError1Skill activated."
