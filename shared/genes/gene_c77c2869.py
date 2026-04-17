"""
Skill that automatically detects when a user reports a broken state with error code 0 and attempts to resolve it by running diagnostic steps and applying known fix strategies.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixbrokenerrorzero(GeneBase):
    gene_id = "gene_c77c2869"
    name = "FixBrokenErrorZero"
    description = """Skill that automatically detects when a user reports a broken state with error code 0 and attempts to resolve it by running diagnostic steps and applying known fix strategies."""
    trigger = "User says something like 'this is broken, please fix error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_message = context.get('user_message', '')
        if 'broken' in user_message.lower() and 'error 0' in user_message.lower():
            self.logger.info('Detected request to fix error 0.')
            # Gather environment information
            env_info = self.get_environment()
            self.logger.debug('Environment info: ' + str(env_info))
            # Define known fix strategies for error 0
            strategies = [self._clear_temp_files, self._restart_service, self._revert_last_change]
            fix_applied = False
            for strategy in strategies:
                try:
                    result = strategy()
                    if result.get('success'):
                        fix_applied = True
                        self.logger.info('Fix applied: ' + result.get('description'))
                        self.speak('I applied a fix: ' + result.get('description'))
                        break
                except Exception as e:
                    self.logger.warning('Strategy ' + strategy.__name__ + ' failed: ' + str(e))
            if not fix_applied:
                self.speak('I could not automatically fix error 0. Please provide more details or contact support.')
        else:
            self.speak('I am not sure what you need help with. Please describe the issue.')
        return "Gene FixBrokenErrorZero activated."