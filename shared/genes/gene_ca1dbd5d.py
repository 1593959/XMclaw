"""
Skill that automatically handles user reports of error 4 by diagnosing the issue, applying known fixes, and reporting the outcome.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_ca1dbd5d"
    name = "FixError4"
    description = """Skill that automatically handles user reports of error 4 by diagnosing the issue, applying known fixes, and reporting the outcome."""
    trigger = "fix error 4|error 4|this is broken"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info('Received request to fix error 4')
        error_details = context.get('error_details', {})
        if not error_details:
            error_details = {}
        error_type = error_details.get('type', 'unknown')
        if error_type == 'error_4':
            try:
                # Step 1: Reset component
                reset_component('component_A')
                # Step 2: Update configuration
                update_config('component_A', {'retry': True})
                # Step 3: Verify fix
                if verify_component('component_A'):
                    logger.info('Error 4 successfully fixed')
                    context['response'] = 'Error 4 has been fixed.'
                else:
                    logger.warning('Fix attempted but verification failed')
                    context['response'] = 'Attempted fix for error 4 but verification failed.'
            except Exception as e:
                logger.error('Exception while fixing error 4: %s', e)
                context['response'] = 'Failed to fix error 4 due to an exception.'
        else:
            logger.warning('Error 4 not found in context')
            context['response'] = 'No error 4 detected.'
        return "Gene FixError4 activated."
