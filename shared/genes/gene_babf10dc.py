"""
Skill that handles user reports of broken functionality and attempts to fix error 3.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_babf10dc"
    name = "FixError3Skill"
    description = """Skill that handles user reports of broken functionality and attempts to fix error 3."""
    trigger = "fix error 3|error 3|broken.*error 3"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve the conversation context
        context = self.get_context()
        # Log the reported issue
        logger.error("User reported error 3: %s", context.message)
        # Run diagnostic for error code 3
        diagnostics = self.run_diagnostics(error_code="3")
        if diagnostics.success:
            self.apply_fix(diagnostics.solution)
            response = "Error 3 has been fixed. Please try again."
        else:
            response = "Unable to automatically resolve error 3. Please contact support."
        # Send the response back to the user
        self.send_message(response)
        return True
        return "Gene FixError3Skill activated."