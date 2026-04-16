"""
Skill that automatically addresses user-reported error 3 by logging, diagnosing, and applying a fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_2da0651f"
    name = "FixError3Skill"
    description = """Skill that automatically addresses user-reported error 3 by logging, diagnosing, and applying a fix."""
    trigger = "User says: "this is broken, please fix error 3""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Log the error
        context.log("Attempting to fix error 3...")
        # Retrieve error details
        error_info = context.get_last_error()
        if error_info and error_info.code == 3:
            # Apply the fix
            context.run_fix("error_3_fix")
            context.log("Error 3 has been resolved.")
        else:
            context.log("Error 3 not detected.")
        return "Gene FixError3Skill activated."
