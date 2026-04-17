"""
Skill to automatically detect and resolve error 4 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error4fixer(GeneBase):
    gene_id = "gene_40b03ff6"
    name = "Error4Fixer"
    description = """Skill to automatically detect and resolve error 4 reported by users."""
    trigger = "error 4"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.info("Attempting to fix error 4...")
        try:
            service = context.get_service("example_service")
            service.reset()
            logger.info("Error 4 resolved.")
            return {"status": "success", "message": "Error 4 fixed"}
        except Exception as e:
            logger.error("Failed to fix error 4", exc_info=True)
            raise
        return "Gene Error4Fixer activated."