"""
Skill that automatically resolves user‑reported error 2 by diagnosing the issue, retrying the failed operation, logging steps, and confirming the fix to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_f2f98614"
    name = "FixError2Skill"
    description = """Skill that automatically resolves user‑reported error 2 by diagnosing the issue, retrying the failed operation, logging steps, and confirming the fix to the user."""
    trigger = "User message contains 'fix error 2' or system detects error code 2."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Log the reported error
        logging.error('Error 2 reported by user: %s', context.get('error_details', ''))
        
        # Retrieve the failing operation from the context
        operation = context.get('operation')
        if not operation:
            context['status'] = 'failed'
            context['response'] = 'No operation found to retry for error 2.'
            return
        
        # Attempt to fix by retrying the operation
        try:
            result = operation.retry(attempts=3)
            logging.info('Successfully retried operation for error 2.')
            context['status'] = 'fixed'
            context['response'] = 'Error 2 has been resolved. The operation was retried successfully.'
        except Exception as e:
            logging.exception('Failed to fix error 2 after retries.')
            context['status'] = 'failed'
            context['response'] = f'Unable to fix error 2: {str(e)}'
        return "Gene FixError2Skill activated."
