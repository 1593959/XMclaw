"""
A skill that automatically resolves error 4 when a user reports that something is broken and asks to fix the error.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_36f4b4e7"
    name = "FixError4Skill"
    description = """A skill that automatically resolves error 4 when a user reports that something is broken and asks to fix the error."""
    trigger = "User says: "this is broken, please fix error 4""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info('Attempting to fix error 4...')
        # Retrieve error details from context or default to a generic description
        error_details = context.get('error_details', {'code': 4, 'message': 'Generic error 4'})
        logger.error(f'Error 4 encountered: {error_details}')
        
        # Step 1: Clear temporary cache that may be causing the issue
        cache_service.clear(key='temp_cache')
        
        # Step 2: Restart the relevant service that reported the error
        service_manager.restart(service_name='example_service')
        
        # Step 3: Verify the service is back up
        if service_manager.is_running(service_name='example_service'):
            logger.info('Error 4 resolved successfully.')
            context['status'] = 'resolved'
            user.notify('Error 4 has been fixed. Please let us know if you experience any further issues.')
        else:
            logger.warning('Error 4 could not be fully resolved; manual intervention may be required.')
            context['status'] = 'partial'
            user.notify('We have taken steps to address error 4, but it may require further attention. Our support team will follow up.')
        return "Gene FixError4Skill activated."
