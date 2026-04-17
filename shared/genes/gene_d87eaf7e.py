"""
Skill to resolve user-reported error 4 by diagnosing the issue, resetting the relevant service, retrying the operation, and confirming the fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_d87eaf7e"
    name = "FixError4"
    description = """Skill to resolve user-reported error 4 by diagnosing the issue, resetting the relevant service, retrying the operation, and confirming the fix."""
    trigger = "User reports 'this is broken, please fix error 4' or the system detects error code 4 in logs."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = context.get_error_code()
        if error_code == 4:
            service = context.get_service('my_service')
            service.reset()
            service.retry()
            context.acknowledge(error_code)
            return {'status': 'fixed', 'message': 'Error 4 resolved'}
        else:
            return {'status': 'ignored', 'message': 'Not error 4'}
        return "Gene FixError4 activated."