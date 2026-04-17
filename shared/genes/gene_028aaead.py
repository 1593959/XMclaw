"""
A skill that automatically diagnoses and resolves the 'error 0' reported by users, providing feedback on the outcome.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorzerofixskill(GeneBase):
    gene_id = "gene_028aaead"
    name = "ErrorZeroFixSkill"
    description = """A skill that automatically diagnoses and resolves the 'error 0' reported by users, providing feedback on the outcome."""
    trigger = "User says 'this is broken, please fix error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            # Step 1: Retrieve context for error 0
            error_context = get_error_context('error 0')
            # Step 2: Attempt to resolve the error
            fix_result = resolve_error_0(error_context)
            # Step 3: Notify the user of successful resolution
            notify_user(f"Error 0 has been resolved: {fix_result}")
        except Exception as e:
            # Log the exception for later analysis
            log_exception(e)
            # Notify the user about the failure
            notify_user("Unable to automatically fix error 0. Please contact support.")
        return "Gene ErrorZeroFixSkill activated."