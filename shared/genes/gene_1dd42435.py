"""
Skill that attempts to resolve error 3 reported by the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3(GeneBase):
    gene_id = "gene_1dd42435"
    name = "FixError3"
    description = """Skill that attempts to resolve error 3 reported by the user."""
    trigger = "fix error 3"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            import logging
            logger = logging.getLogger(__name__)
            logger.info("Attempting to fix error 3...")
            error_details = context.get('error_details', {})
            service_name = error_details.get('service_name')
            clear_cache_and_restart(service_name)
            logger.info("Error 3 has been resolved.")
            return {"status": "success", "message": "Error 3 fixed."}
        except Exception as e:
            logger.error(f"Failed to fix error 3: {e}")
            return {"status": "error", "message": str(e)}
        return "Gene FixError3 activated."
