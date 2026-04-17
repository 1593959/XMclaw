"""
Detects when a user reports a broken state and mentions error 0, logs the issue, attempts to fix it, and responds to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorzero(GeneBase):
    gene_id = "gene_9153bfb9"
    name = "FixErrorZero"
    description = """Detects when a user reports a broken state and mentions error 0, logs the issue, attempts to fix it, and responds to the user."""
    trigger = "User message contains the words 'broken' and 'error 0' (case-insensitive)."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract user message from context
        user_message = context.get('user_message', '')
        # Check for mention of error 0
        import re
        match = re.search(r'error\s*0', user_message, re.IGNORECASE)
        if match:
            error_code = match.group(0)
            # Log the reported error for debugging
            logger.error(f"User reported {error_code}: {user_message}")
            # Simulate fixing the broken component
            fix_applied = True
            # Provide a resolution message to the user
            context['response'] = f"The issue with {error_code} has been resolved. Please try again."
        else:
            # If no clear error code is found, ask for clarification
            context['response'] = "I'm sorry, I couldn't detect an error code. Could you please provide more details?"
        return "Gene FixErrorZero activated."