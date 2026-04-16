"""
Skill that resolves the reported error 3.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_c70f7d90"
    name = "FixError3Skill"
    description = """Skill that resolves the reported error 3."""
    trigger = "User says 'this is broken, please fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error("Attempting to fix error 3")
        try:
            error_details = self.diagnose_error_3()
            if error_details:
                self.apply_fix(error_details)
                return {"status": "success", "message": "Error 3 has been fixed."}
            else:
                raise ValueError("Unable to locate error 3")
        except Exception as e:
            logger.exception("Error while fixing error 3")
            raise
        return "Gene FixError3Skill activated."
