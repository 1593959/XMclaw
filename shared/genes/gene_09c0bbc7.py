"""
Skill that automatically diagnoses and resolves error 2 when a user reports 'this is broken, please fix error 2'.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_09c0bbc7"
    name = "FixError2Skill"
    description = """Skill that automatically diagnoses and resolves error 2 when a user reports 'this is broken, please fix error 2'."""
    trigger = "User message contains 'fix error 2' or 'this is broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            # Retrieve details for error 2
            error_info = context.get_error_details(error_code=2)
            if error_info:
                # Apply the configured fix strategy
                fix_result = fix_engine.apply(error_info)
                context.update_state(success=True, result=fix_result)
                context.log(f"Error 2 successfully resolved: {fix_result}")
            else:
                context.update_state(success=False, error="Error 2 not found")
                context.log("Error 2 could not be located.")
        except Exception as e:
            context.log(f"Failed to fix error 2: {e}")
            context.update_state(success=False, error=str(e))
        return "Gene FixError2Skill activated."