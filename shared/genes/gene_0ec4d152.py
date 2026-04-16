"""
Responds to user reports of error 3, logs the issue, gathers additional details, checks for known fix, and either applies fix or escalates.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_0ec4d152"
    name = "FixError3Skill"
    description = """Responds to user reports of error 3, logs the issue, gathers additional details, checks for known fix, and either applies fix or escalates."""
    trigger = "User message contains 'error 3' or 'fix error 3'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error(f"User reported error 3: {user_text}")
        self.ask_for_details(user_text)
        if self.has_known_fix("error3"):
            self.apply_fix("error3")
        else:
            self.escalate()
        return "Gene FixError3Skill activated."
