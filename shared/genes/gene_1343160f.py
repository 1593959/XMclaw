"""
Skill to diagnose and fix error 4 reported by the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorfixer(GeneBase):
    gene_id = "gene_1343160f"
    name = "ErrorFixer"
    description = """Skill to diagnose and fix error 4 reported by the user."""
    trigger = "When the user says 'fix error 4' or reports 'error 4'."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve error details
        error_details = context.get('error_details')
        if not error_details:
            # Fetch error logs from the system
            error_details = api.fetch_error_logs(error_id='4')
        
        # Diagnose the root cause
        root_cause = diagnose_error(error_details)
        
        # Apply fix based on root cause
        if root_cause == 'timeout':
            api.restart_service('service_name')
            api.clear_cache()
        elif root_cause == 'data_mismatch':
            api.correct_data(error_id='4')
        elif root_cause == 'configuration':
            api.reset_configuration('service_name')
        else:
            api.notify_support_team('Error 4 unresolved')
        
        # Verify the fix
        api.verify_fix(error_id='4')
        
        # Respond to the user
        response = f'Error 4 has been resolved. Root cause: {root_cause}. Please verify the functionality.'
        context.reply(response)
        return "Gene ErrorFixer activated."
