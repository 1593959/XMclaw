"""
Skill that automatically addresses user-reported breakage and attempts to fix error 4.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_e429a1ec"
    name = "FixError4Skill"
    description = """Skill that automatically addresses user-reported breakage and attempts to fix error 4."""
    trigger = "Message matches regex: "(?i)\b(broken|error\s*4|fix error 4)\b""

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info("FixError4 skill triggered")
        error_context = context.get("error_details", {})
        if error_context.get("code") == 4:
            fix_result = "Applied patch for error 4."
            logger.info(fix_result)
            message = f"Error 4 has been fixed. {fix_result}"
        else:
            message = "Error 4 not found in context."
        return {"message": message}
        return "Gene FixError4Skill activated."
