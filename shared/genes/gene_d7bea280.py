"""
Automatically diagnoses and fixes reported error 0 (e.g., a broken service) by restarting the relevant component and logging the outcome.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0skill(GeneBase):
    gene_id = "gene_d7bea280"
    name = "FixError0Skill"
    description = """Automatically diagnoses and fixes reported error 0 (e.g., a broken service) by restarting the relevant component and logging the outcome."""
    trigger = "user_reported_error_0"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            # Diagnose the reported error 0
            error_info = self.get_error_info('0')
            if error_info:
                # Attempt to restart the service associated with error 0
                self.restart_service(error_info['service'])
                self.log('Service restarted successfully after fixing error 0.')
            else:
                self.log('No error 0 found to fix.')
        except Exception as e:
            self.log('Failed to fix error 0: ' + str(e))
            raise
        return "Gene FixError0Skill activated."