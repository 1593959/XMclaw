"""
Skill to automatically address the user complaint 'this is broken, please fix error 3' by diagnosing the broken component, attempting a repair, and confirming resolution.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error3fixskill(GeneBase):
    gene_id = "gene_5f35b01b"
    name = "Error3FixSkill"
    description = """Skill to automatically address the user complaint 'this is broken, please fix error 3' by diagnosing the broken component, attempting a repair, and confirming resolution."""
    trigger = "error_3_reported"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            component = context.get('broken_component')
            error_details = context.get('error_details', {})
            logger.info(f'Received request to fix error 3 for component: {component} details: {error_details}')
            diagnosis = diagnostic_service.analyze(component, error_details)
            if diagnosis.get('fixable'):
                repair_result = repair_service.apply_fix(component, diagnosis['solution'])
                user.notify(f'Error 3 has been resolved for {component}. Result: {repair_result}')
            else:
                user.notify(f'Error 3 could not be automatically fixed for {component}. Escalating to support.')
                support.escalate(component, error_details)
        except Exception as e:
            logger.exception('Failed to fix error 3')
            user.notify('An unexpected error occurred while fixing error 3. Please try again later.')
            raise
        return "Gene Error3FixSkill activated."