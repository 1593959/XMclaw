"""
Automatically handles user reports of error 4 by diagnosing the issue and attempting a fix
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorfixgene(GeneBase):
    gene_id = "gene_70ec7f91"
    name = "ErrorFixGene"
    description = """Automatically handles user reports of error 4 by diagnosing the issue and attempting a fix"""
    trigger = "User message contains 'error 4' or reports 'broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            logger.error("User reported error 4: %s", details)
            # Run diagnostic for error 4
            diagnostic_result = run_diagnostic(error_code="4")
            if diagnostic_result:
                # Apply the fix
                apply_fix(diagnostic_result)
                notify_user("Error 4 has been resolved.")
            else:
                # Escalate to support
                escalate_to_support(details)
        except Exception as e:
            logger.exception("Failed to handle error 4")
            notify_user("An error occurred while fixing error 4. Support has been alerted.")
        return "Gene ErrorFixGene activated."
