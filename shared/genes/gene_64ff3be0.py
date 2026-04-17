"""
Automatically handles user reports of 'error 4', diagnosing the issue and performing the appropriate remediation.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_64ff3be0"
    name = "FixError4Skill"
    description = """Automatically handles user reports of 'error 4', diagnosing the issue and performing the appropriate remediation."""
    trigger = "User message contains 'error 4' or 'fix error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_id = '4'
        logger.warning(f'User reported error {error_id}')
        # Fetch error details from internal store
        error_details = error_store.get(error_id)
        if error_details:
            # Identify the service associated with the error
            service_name = error_details.get('service_name')
            service = get_service(service_name)
            # Reset the service to clear the error
            service.reset()
            return 'Error 4 has been fixed. The service was reset successfully.'
        else:
            return 'Unable to locate details for error 4. Please provide more information.'
        return "Gene FixError4Skill activated."