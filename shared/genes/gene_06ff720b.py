"""Auto-generated Gene for XMclaw.
Triggers an escalation workflow when a user reports that a previously fixed bug has re-occurred, ensuring a deeper investigation and preventing endless re-fix loops.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Reopenedbugescalation(GeneBase):
    gene_id = "gene_06ff720b"
    name = "ReopenedBugEscalation"
    description = "Triggers an escalation workflow when a user reports that a previously fixed bug has re-occurred, ensuring a deeper investigation and preventing endless re-fix loops."
    trigger = "{'type': 'user_report', 'condition':"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene ReopenedBugEscalation activated."
