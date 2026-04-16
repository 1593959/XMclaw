"""
Skill that automatically resolves error 2 when a user reports 'this is broken, please fix error 2'. It listens for the specific phrase or error code, diagnoses the issue, and runs remediation steps to restore normal operation.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_ba40f788"
    name = "FixError2Skill"
    description = """Skill that automatically resolves error 2 when a user reports 'this is broken, please fix error 2'. It listens for the specific phrase or error code, diagnoses the issue, and runs remediation steps to restore normal operation."""
    trigger = "User says 'this is broken, please fix error 2' or error code 2 is detected in logs."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_info = context.get('error_info', {})
        error_code = error_info.get('code')
        if error_code == 2:
            logger.info('Detected error 2, starting remediation...')
            # Restart the impacted service
            restart_service('example_service')
            # Clear temporary files that may be causing the error
            clear_temp_files()
            # Verify service health after restart
            if check_service_health('example_service'):
                logger.info('Error 2 resolved successfully.')
                return {'status': 'fixed'}
            else:
                logger.error('Failed to resolve error 2.')
                return {'status': 'failed'}
        else:
            logger.warning('No error 2 detected.')
            return {'status': 'ignored'}
        return "Gene FixError2Skill activated."
