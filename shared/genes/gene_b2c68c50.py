"""Auto-generated Gene for XMclaw.
When a user reports a bug (issue with label "bug"), automatically create a dedicated fix branch, assign the issue to the appropriate developer, and notify the team so the bug can be addressed promptly.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class AutoBugFixWorkflow(GeneBase):
    gene_id = "gene_b2c68c50"
    name = "Auto Bug Fix Workflow"
    description = "When a user reports a bug (issue with label \"bug\"), automatically create a dedicated fix branch, assign the issue to the appropriate developer, and notify the team so the bug can be addressed prompt"
    trigger = "{'type': 'issue_created', 'conditions': {'labels': {'contains': 'bug'}}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Auto Bug Fix Workflow activated."
