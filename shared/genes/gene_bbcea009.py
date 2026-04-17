"""Auto-generated Gene for XMclaw.
Automatically creates a bug ticket and notifies the development team when a user reports a bug.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class BugReportHandlingGene(GeneBase):
    gene_id = "gene_bbcea009"
    name = "Bug Report Handling Gene"
    description = "Automatically creates a bug ticket and notifies the development team when a user reports a bug."
    trigger = "{'type': 'user_reported_issue', 'filters': {'category': 'bug'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Bug Report Handling Gene activated."
