"""
A skill that automatically resolves error 2 reported by users when they say 'this is broken, please fix error 2'.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_5f6bb0cd"
    name = "FixError2Skill"
    description = """A skill that automatically resolves error 2 reported by users when they say 'this is broken, please fix error 2'."""
    trigger = "User input contains the phrase 'this is broken, please fix error 2'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve error details
        error_info = context.get('error_info')
        if not error_info:
            raise ValueError('No error information provided')
        logger.info(f'Attempting to fix error 2: {error_info}')
        # Call the error‑fixing routine
        result = fix_error_2(error_info)
        # Return success message
        return {'status': 'fixed', 'details': result}
        return "Gene FixError2Skill activated."
