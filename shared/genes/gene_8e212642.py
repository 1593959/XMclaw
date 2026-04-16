"""
Skill that automatically resolves error code 0 reported by the user. It runs a diagnostic routine, resets the affected component, and returns a resolution status.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorzero(GeneBase):
    gene_id = "gene_8e212642"
    name = "FixErrorZero"
    description = """Skill that automatically resolves error code 0 reported by the user. It runs a diagnostic routine, resets the affected component, and returns a resolution status."""
    trigger = "User message contains 'error 0' or the phrase 'this is broken' and explicitly requests a fix."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = context.get('error_code') or 0
        if error_code == 0:
            logger.info('Error 0 reported. Initiating diagnostic...')
            component = system.get_component(context.get('component_id'))
            component.reset()
            return {'status': 'resolved', 'message': 'Error 0 has been fixed.'}
        else:
            logger.warning('No error 0 detected, skipping fix.')
            return {'status': 'skipped', 'message': 'No action taken.'}
        return "Gene FixErrorZero activated."
