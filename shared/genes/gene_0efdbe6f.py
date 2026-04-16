"""
Skill to resolve error 4 reported by the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_0efdbe6f"
    name = "FixError4Skill"
    description = """Skill to resolve error 4 reported by the user."""
    trigger = "User says 'this is broken, please fix error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        diagnostic_result = self.run_diagnostics()
        if 'error_4' in diagnostic_result:
            self.apply_fix('error_4')
            return {'status': 'fixed', 'error': 'error_4'}
        else:
            return {'status': 'error_not_found'}
        return "Gene FixError4Skill activated."
