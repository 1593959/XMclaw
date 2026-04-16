"""
Skill that automatically handles user reports of error 0, diagnosing the root cause and providing a fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0skill(GeneBase):
    gene_id = "gene_e70b2c5b"
    name = "FixError0Skill"
    description = """Skill that automatically handles user reports of error 0, diagnosing the root cause and providing a fix."""
    trigger = "User message contains 'error 0' or 'this is broken, please fix error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error('Error 0 reported by user')
        # Diagnose the issue
        issue = diagnose_error_0()
        if issue:
            fix_result = fix_issue(issue)
            return fix_result
        else:
            return 'Unable to resolve error 0. Please contact support.'
        return "Gene FixError0Skill activated."
