"""
Skill that listens for user reports of a broken component and attempts to resolve error 3 by running diagnostics and applying a targeted fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_cb539617"
    name = "FixError3Skill"
    description = """Skill that listens for user reports of a broken component and attempts to resolve error 3 by running diagnostics and applying a targeted fix."""
    trigger = "User message contains "this is broken, please fix error 3""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            # Extract user message
            user_message = context.get("user_message", "")
            if "error 3" in user_message.lower():
                logger.info("Fix request for error 3 detected.")
                # Run diagnostic
                diag_result = run_diagnostics(context)
                # Apply targeted fix
                fix_result = apply_fix_for_error_3(diag_result)
                return {"status": "fixed", "details": fix_result}
            else:
                logger.warning("No error 3 fix requested.")
                return {"status": "ignored"}
        except Exception as e:
            logger.error(f"Failed to fix error 3: {e}")
            return {"status": "error", "message": str(e)}
        return "Gene FixError3Skill activated."
