"""
Skill that automatically detects and resolves error code 4 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_3a8aab9a"
    name = "FixError4Skill"
    description = """Skill that automatically detects and resolves error code 4 reported by users."""
    trigger = "['error 4', 'fix error 4', 'error 4 reported']"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger = logging.getLogger(__name__)
        try:
            logger.error(f"User reported error 4: {context.get('error_details')}")
            diagnostic_result = self.run_diagnostic(error_code="4")
            if diagnostic_result.get("fixed"):
                logger.info("Error 4 resolved automatically.")
                return {"status": "success", "message": "Error 4 fixed."}
            else:
                logger.warning("Could not automatically fix error 4.")
                return {"status": "failed", "message": "Manual intervention required."}
        except Exception as e:
            logger.exception("Unexpected exception while fixing error 4")
            return {"status": "error", "message": str(e)}
        return "Gene FixError4Skill activated."