"""
A skill that logs user-reported errors and attempts to apply a known fix. Triggered when the user mentions a broken state or an error number (e.g., 'error 2').
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorfixskill(GeneBase):
    gene_id = "gene_5330ba74"
    name = "ErrorFixSkill"
    description = """A skill that logs user-reported errors and attempts to apply a known fix. Triggered when the user mentions a broken state or an error number (e.g., 'error 2')."""
    trigger = "User message contains 'error' or 'broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_number = int(context.get('error_id', '0').replace('error ', ''))
        log_message = 'User reported error {}: {}'.format(error_number, context.get('user_message', ''))
        logging.warning(log_message)
        fix_map = {
            2: 'restart_service',
        }
        fix_action = fix_map.get(error_number)
        if fix_action:
            result = execute_fix(fix_action)
        else:
            result = 'No known fix for error ' + str(error_number)
        return result
        return "Gene ErrorFixSkill activated."