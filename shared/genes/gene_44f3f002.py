"""
Skill to acknowledge and log user reports about error 2. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Handleerror2report(GeneBase):
    gene_id = "gene_44f3f002"
    name = "HandleError2Report"
    description = """Skill to acknowledge and log user reports about error 2."""
    trigger = "User input matches patterns like 'error 2', 'broken', or 'fix error'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_message = context.user_message
        logger.info("User reported issue: %s", user_message)
        response = "I'm sorry to hear that. I'm looking into error 2 now. Could you please provide more details (e.g., steps to reproduce)?"
        context.send_message(response)
        return "Gene HandleError2Report activated."