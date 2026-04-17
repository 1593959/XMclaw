"""
Handles user reports of error 4 by logging the issue, extracting context, attempting a fix, and responding with the result. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_7dadef43"
    name = "FixError4"
    description = """Handles user reports of error 4 by logging the issue, extracting context, attempting a fix, and responding with the result."""
    trigger = "User mentions ‘error 4' or says ‘broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Log the reported error
        log.error('User reported error 4: ' + user_message)
        # Extract error context
        error_context = extract_error_context(user_message)
        # Attempt to fix error 4
        fix_result = fix_error_4(error_context)
        # Respond to the user
        return f'Error 4 fixed: {fix_result}'
        return "Gene FixError4 activated."