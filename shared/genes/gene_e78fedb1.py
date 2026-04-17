"""
Skill that automatically resolves error 2 when the user reports it as broken.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_e78fedb1"
    name = "FixError2Skill"
    description = """Skill that automatically resolves error 2 when the user reports it as broken."""
    trigger = "User says 'fix error 2' or mentions 'error 2'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error("Error 2 detected, attempting fix...")
        try:
            # Perform diagnostic steps
            diagnostic_result = run_diagnostic('error_2')
            if diagnostic_result['success']:
                logger.info("Diagnostic passed. Applying fix.")
                apply_fix('error_2')
                logger.info("Fix applied successfully.")
            else:
                logger.warning("Diagnostic failed. Notify support.")
                notify_support(diagnostic_result['error'])
        except Exception as e:
            logger.exception("Failed to fix error 2")
            notify_support(str(e))
        return "Gene FixError2Skill activated."