"""
Skill that automatically detects and resolves user-reported breakage labeled as error 2.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_c77f8306"
    name = "FixError2Skill"
    description = """Skill that automatically detects and resolves user-reported breakage labeled as error 2."""
    trigger = "User mentions 'error 2' or 'broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve error details from context
        error_details = context.get('error_details', {})
        error_code = error_details.get('code')
        if error_code == 2:
            # Perform fix for error 2
            self.apply_fix_for_error_2(error_details)
            return {'status': 'fixed', 'code': 2}
        else:
            raise ValueError('Unsupported error code')
        return "Gene FixError2Skill activated."