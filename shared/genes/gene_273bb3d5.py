"""
Skill that automatically diagnoses and resolves user-reported 'error 0', applying known fixes or escalating to support.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixbrokenerror0(GeneBase):
    gene_id = "gene_273bb3d5"
    name = "FixBrokenError0"
    description = """Skill that automatically diagnoses and resolves user-reported 'error 0', applying known fixes or escalating to support."""
    trigger = "user_report_error0"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve user report context
        user_message = context.get('user_message', '')
        # Extract error code from message
        error_code = extract_error_code(user_message)
        if error_code == 0:
            logger.error(f'User reported error 0: {user_message}')
            # Fetch diagnostic report from the system
            diagnostic = system.get_diagnostic_report()
            # If a known fix is available, apply it
            if diagnostic.get('fix_available'):
                system.apply_fix(diagnostic['fix_id'])
                return {'status': 'resolved', 'message': 'Error 0 fixed automatically.'}
            else:
                # Escalate to support team
                support_team.notify('Error 0 requires manual intervention', diagnostic)
                return {'status': 'escalated', 'message': 'Issue escalated to support.'}
        else:
            return {'status': 'skipped', 'message': 'No error 0 detected.'}
        return "Gene FixBrokenError0 activated."