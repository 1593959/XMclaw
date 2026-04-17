"""
Skill that automatically addresses user reports of error 4 being broken, diagnosing the issue and applying a known fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_c05f1dfe"
    name = "FixError4"
    description = """Skill that automatically addresses user reports of error 4 being broken, diagnosing the issue and applying a known fix."""
    trigger = "message.content matches /error\\s?4|broken/i"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Log the user report
        logger.info("User reported error 4: %s", message.content)
        # Locate error 4 context from the logs
        error_context = error_log.find_error(4)
        if not error_context:
            user.reply("I couldn't locate error 4 in the logs. Please provide more details.")
            return
        # Apply the remediation for error 4
        fix_applied = fix_manager.apply_fix("error_4_fix", context=error_context)
        if fix_applied:
            user.reply("Error 4 has been fixed. Please verify the behavior.")
        else:
            user.reply("I was unable to automatically fix error 4. Escalating to support.")
        return "Gene FixError4 activated."