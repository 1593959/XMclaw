"""
Skill that detects when a user reports a broken state with error 4, attempts to diagnose the issue using the knowledge base, and returns a fix or guidance.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_ea26e120"
    name = "FixError4"
    description = """Skill that detects when a user reports a broken state with error 4, attempts to diagnose the issue using the knowledge base, and returns a fix or guidance."""
    trigger = "User says something like "this is broken, please fix error 4""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract error code from user message
        user_message = context.user_message
        error_code = '4'
        # Log the reported issue
        logging.warning(f'Reported issue: {user_message}')
        # Attempt to locate a known fix for error 4
        fix = find_known_fix('error_4')
        if fix:
            response = fix
        else:
            response = 'I could not locate a direct fix for error 4. Please provide additional details.'
        # Send the response back to the user
        context.send_message(response)
        return "Gene FixError4 activated."
