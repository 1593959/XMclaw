"""
Skill to automatically diagnose and resolve user-reported 'error 2' issues.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_8ced56f9"
    name = "FixError2Skill"
    description = """Skill to automatically diagnose and resolve user-reported 'error 2' issues."""
    trigger = "User reports 'this is broken, please fix error 2' or similar phrase."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = "2"
        logger.info("Received request to fix error %s", error_code)
        error_details = self.system.get_error_details(error_code)
        if not error_details:
            logger.warning("No details found for error %s", error_code)
            return {"status": "failed", "message": "Error details not found"}
        fix_applied = self.system.apply_fix(error_details)
        if fix_applied:
            logger.info("Fix applied successfully for error %s", error_code)
            self.notify_user(context.user_id, "Error 2 has been resolved.")
            return {"status": "success"}
        else:
            logger.error("Failed to apply fix for error %s", error_code)
            self.notify_user(context.user_id, "Could not resolve error 2. Please contact support.")
            return {"status": "failed"}
        return "Gene FixError2Skill activated."
