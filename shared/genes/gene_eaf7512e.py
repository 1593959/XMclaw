"""
Skill that automatically addresses error 1 when a user reports 'this is broken, please fix error 1'.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror1skill(GeneBase):
    gene_id = "gene_eaf7512e"
    name = "FixError1Skill"
    description = """Skill that automatically addresses error 1 when a user reports 'this is broken, please fix error 1'."""
    trigger = "User message contains 'broken' and 'error 1'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract the user's report
        user_report = context.get('user_message', '')
        if 'error 1' in user_report.lower():
            # Run diagnostic
            diagnostic = diagnostic_tool.run()
            if diagnostic.get('error_found'):
                # Apply the fix
                fix_result = fix_tool.apply(diagnostic['error_id'])
                return {'status': 'fixed', 'details': fix_result}
            else:
                return {'status': 'no_issue_found'}
        else:
            return {'status': 'not_applicable'}
        return "Gene FixError1Skill activated."