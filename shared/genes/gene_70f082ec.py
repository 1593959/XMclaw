"""
Skill to automatically diagnose and fix error 4 reported by the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_70f082ec"
    name = "FixError4Skill"
    description = """Skill to automatically diagnose and fix error 4 reported by the user."""
    trigger = "User message contains 'error 4' or 'this is broken, please fix error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            logger.error('Error 4 detected, initiating fix...')
            # Identify the root cause (example: missing configuration)
            cause = diagnose_error_4()
            if cause == 'missing_config':
                # Apply configuration fix
                apply_config_fix()
            elif cause == 'service_down':
                # Restart the affected service
                restart_service('target_service')
            else:
                # Generic fallback fix
                run_generic_fix('error_4')
            logger.info('Error 4 successfully fixed.')
            return {'status': 'success', 'message': 'Error 4 has been resolved.'}
        except Exception as e:
            logger.exception('Failed to fix error 4')
            return {'status': 'error', 'message': str(e)}
        return "Gene FixError4Skill activated."