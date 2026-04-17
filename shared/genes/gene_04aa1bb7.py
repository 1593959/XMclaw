"""Auto-generated Gene for XMclaw.
Triggers when a user reports a bug and explicitly asks for it to be fixed again, indicating a recurring or unresolved issue.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class FixBugOnceMore(GeneBase):
    gene_id = "gene_04aa1bb7"
    name = "Fix Bug Once More"
    description = "Triggers when a user reports a bug and explicitly asks for it to be fixed again, indicating a recurring or unresolved issue."
    trigger = "{'type': 'user_reported_issue', 'issue_type': 'bug', 'request': 'fix', 'reiteration': True}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Fix Bug Once More activated."
