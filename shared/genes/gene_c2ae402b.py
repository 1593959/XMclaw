"""Auto-generated Gene for XMclaw.
When a user reports a bug, automatically create a bug ticket, assign it to the development team, and notify the relevant stakeholders.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Bugfixworkflow(GeneBase):
    gene_id = "gene_c2ae402b"
    name = "BugFixWorkflow"
    description = "When a user reports a bug, automatically create a bug ticket, assign it to the development team, and notify the relevant stakeholders."
    trigger = "{'type': 'user_reported_issue', 'condition':"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene BugFixWorkflow activated."
