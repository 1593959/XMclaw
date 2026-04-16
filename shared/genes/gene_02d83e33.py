"""
Skill to handle user reports about error 4, retrieve a known fix, apply it, and confirm to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error4fixskill(GeneBase):
    gene_id = "gene_02d83e33"
    name = "Error4FixSkill"
    description = """Skill to handle user reports about error 4, retrieve a known fix, apply it, and confirm to the user."""
    trigger = "User says 'this is broken, please fix error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:\n    logger.error('User reported error 4: broken')\n    error_info = error_service.get_error_details(4)\n    fix = fix_service.get_fix_for_error(error_info)\n    fix.apply()\n    messaging.send_message(user_id, 'Error 4 has been fixed.')\nexcept Exception as e:\n    logger.exception('Failed to fix error 4')\n    messaging.send_message(user_id, 'Unable to fix error 4. Please contact support.')
        return "Gene Error4FixSkill activated."
