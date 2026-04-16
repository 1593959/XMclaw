"""
Automatically diagnose and fix error 2 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2(GeneBase):
    gene_id = "gene_e073e5c5"
    name = "FixError2"
    description = """Automatically diagnose and fix error 2 reported by users."""
    trigger = "User input contains phrases like 'this is broken, please fix error 2' or 'error 2' with a request to fix."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_details = self.get_error_details(2)
        self.logger.info('Diagnosing error 2: %s', error_details)
        if error_details.get('type') == 'timeout':
            self.restart_service('service_a')
        elif error_details.get('type') == 'null_pointer':
            self.fix_config('service_a', 'enable_null_checks', True)
        else:
            self.notify_team('Unhandled error 2', error_details)
        if self.verify_error_resolved(2):
            self.logger.info('Error 2 successfully fixed.')
            return {'status': 'fixed', 'error_id': 2}
        else:
            self.logger.error('Failed to resolve error 2.')
            return {'status': 'failed', 'error_id': 2}
        return "Gene FixError2 activated."
