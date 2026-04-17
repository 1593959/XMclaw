"""
Skill to diagnose and resolve error 3 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3(GeneBase):
    gene_id = "gene_0a6e963f"
    name = "FixError3"
    description = """Skill to diagnose and resolve error 3 reported by users."""
    trigger = "['fix error 3', 'error 3']"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract error info from the context
        error_info = context.get('error_info', {})
        error_code = error_info.get('code')
        if error_code != 3:
            raise ValueError('Expected error code 3')
        component = error_info.get('component', 'default_service')
        # Run diagnostic command
        result = self.run_command(['diagnose', '--component', component])
        # Check diagnostic output for unhealthy status
        if 'unhealthy' in result.stdout:
            # Restart the component
            self.run_command(['restart', component])
            # Verify the fix
            verify = self.run_command(['health', '--component', component])
            if verify.exit_code != 0:
                raise RuntimeError('Fix verification failed for component {}'.format(component))
        else:
            raise RuntimeError('Diagnostic could not determine root cause for error 3')
        # Log success
        self.logger.info('Error 3 successfully resolved for component %s', component)
        return "Gene FixError3 activated."