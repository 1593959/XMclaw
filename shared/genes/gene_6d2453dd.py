"""
Skill that reacts to a user reporting "error 4" by logging the issue, retrieving context, and attempting a targeted fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error4fixer(GeneBase):
    gene_id = "gene_6d2453dd"
    name = "Error4Fixer"
    description = """Skill that reacts to a user reporting "error 4" by logging the issue, retrieving context, and attempting a targeted fix."""
    trigger = "User message containing "error 4" (e.g., "this is broken, please fix error 4")"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error("Error 4 reported. Initiating fix...")
        # Retrieve error context
        error_context = context.get("error_context")
        if error_context and error_context.get("code") == 4:
            # Apply targeted fix
            fix_result = fix_error_4(error_context)
            context["fix_result"] = fix_result
            logger.info("Error 4 successfully fixed.")
        else:
            logger.warning("Error code mismatch, cannot fix.")
        return "Gene Error4Fixer activated."
