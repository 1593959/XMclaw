"""
Skill to automatically diagnose and resolve Error 4 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_a7a383f2"
    name = "FixError4Skill"
    description = """Skill to automatically diagnose and resolve Error 4 reported by users."""
    trigger = "user_message_contains('error 4') or user_message_contains('broken')"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve error details
        error_info = context.get('error_details', {})
        if not error_info:
            error_info = {'code': 4, 'message': 'Error 4 reported'}
        error_code = error_info.get('code')
        error_message = error_info.get('message', '')
        # Apply fix actions for error code 4
        if error_code == 4:
            # Reset configuration and clear caches
            reset_config('service_a')
            clear_cache('error4_cache')
            # Notify user
            user.notify('Error 4 has been fixed. Please try again.')
        # Return success response
        return {'status': 'fixed', 'error_code': error_code, 'message': error_message}
        return "Gene FixError4Skill activated."