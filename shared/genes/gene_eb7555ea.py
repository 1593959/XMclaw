"""
Skill that automatically handles user reports about a broken system referencing error 4. It logs a high‑priority support ticket, notifies the engineering team in the #engineering channel, and attempts to apply the known fix for error 4.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_eb7555ea"
    name = "FixError4"
    description = """Skill that automatically handles user reports about a broken system referencing error 4. It logs a high‑priority support ticket, notifies the engineering team in the #engineering channel, and attempts to apply the known fix for error 4."""
    trigger = "User message contains the words 'broken' and 'error 4' (case‑insensitive)"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error(f"User reported issue: {user_message}")
        support_ticket = support_api.create_ticket(
            title=f"User reported broken with error 4",
            description=user_message,
            priority="high"
        )
        chat_api.send_message(
            channel="#engineering",
            text=f"New high‑priority ticket created: {support_ticket.id} - {user_message}"
        )
        fix_result = diagnostics.apply_fix("error_4")
        if fix_result.success:
            logger.info("Fix applied successfully")
        else:
            logger.warning("Fix could not be applied automatically; ticket escalated for manual review")
        return "Gene FixError4 activated."
