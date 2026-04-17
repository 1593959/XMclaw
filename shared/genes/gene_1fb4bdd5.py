"""
Listens for user reports of broken functionality that include a specific error code (e.g., "error 1"), logs the issue, runs diagnostics, attempts to fix the error, and notifies support if the fix fails.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorfixskill(GeneBase):
    gene_id = "gene_1fb4bdd5"
    name = "ErrorFixSkill"
    description = """Listens for user reports of broken functionality that include a specific error code (e.g., "error 1"), logs the issue, runs diagnostics, attempts to fix the error, and notifies support if the fix fails."""
    trigger = "{'type': 'regex', 'pattern': 'broken.*error\\s*1'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error("User reported error 1: broken functionality")
        self.run_diagnostics(context)
        try:
            self.fix_error_1(context)
            logger.info("Error 1 fixed successfully")
        except Exception as e:
            logger.exception("Failed to fix error 1: %s", e)
            self.notify_support(context)
        return "Gene ErrorFixSkill activated."