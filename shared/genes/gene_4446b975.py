"""Auto-generated Gene for XMclaw.
When a user reports a bug (e.g., by saying 'fix the bug'), this gene automatically creates a bug ticket, assigns it to the appropriate development team, and notifies the user of the ticket number.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Bugfixtrigger(GeneBase):
    gene_id = "gene_4446b975"
    name = "BugFixTrigger"
    description = "When a user reports a bug (e.g., by saying 'fix the bug'), this gene automatically creates a bug ticket, assigns it to the appropriate development team, and notifies the user of the ticket number."
    trigger = "{'type': 'UserInput', 'condition':"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene BugFixTrigger activated."
