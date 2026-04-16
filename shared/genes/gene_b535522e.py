"""
Detects user reports about broken content that reference error 4 and automatically attempts to resolve the issue by analyzing related logs and applying fixes.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_b535522e"
    name = "FixError4Skill"
    description = """Detects user reports about broken content that reference error 4 and automatically attempts to resolve the issue by analyzing related logs and applying fixes."""
    trigger = "this is broken, please fix error 4"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logs = error_service.get_logs('error_4')
        fixed = []
        unresolved = []
        for log in logs:
            if log.severity == 'error':
                fix_result = fix_engine.apply_fix(log)
                if fix_result.success:
                    log.status = 'fixed'
                    error_service.update_log(log)
                    fixed.append(log.id)
                else:
                    log.status = 'unresolved'
                    error_service.notify_admin(log)
                    unresolved.append(log.id)
        return {
            'fixed': fixed,
            'unresolved': unresolved
        }
        return "Gene FixError4Skill activated."
