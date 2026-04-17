"""
Automatically addresses user reports of 'error 3' by diagnosing the root cause and applying corrective actions.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error3fixskill(GeneBase):
    gene_id = "gene_69479594"
    name = "Error3FixSkill"
    description = """Automatically addresses user reports of 'error 3' by diagnosing the root cause and applying corrective actions."""
    trigger = "User says 'fix error 3' or reports 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_info = context.get("error_details", "unknown")
        logger.error(f"Error 3 reported: {error_info}")
        diagnostic = run_diagnostics(error_code=3)
        if diagnostic == "missing_config":
            apply_default_config()
        elif diagnostic == "timeout":
            increase_timeout()
        else:
            logger.warning("Unknown cause, applying generic fix")
            apply_generic_fix()
        if verify_fix(error_code=3):
            logger.info("Error 3 fixed successfully")
            context["status"] = "resolved"
        else:
            logger.error("Failed to fix error 3")
            context["status"] = "failed"
        return "Gene Error3FixSkill activated."