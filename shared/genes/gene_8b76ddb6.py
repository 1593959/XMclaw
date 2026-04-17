"""Auto-generated Gene for XMclaw.
When a user reports a bug that was previously marked as fixed, this rule automatically reopens the ticket, reassigns it to the original developer, and notifies the team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class ReopenFixedBugOnRepeatedReport(GeneBase):
    gene_id = "gene_8b76ddb6"
    name = "Reopen Fixed Bug on Repeated Report"
    description = "When a user reports a bug that was previously marked as fixed, this rule automatically reopens the ticket, reassigns it to the original developer, and notifies the team."
    trigger = "{'type': 'bug_report', 'conditions': [{'field': 'ticket.status', 'operator': 'equals', 'value': 'Closed'}, {'field': 'ticket.resolution', 'operator': 'equals', 'value': 'Fixed'}, {'field': 'ticket.clo"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Reopen Fixed Bug on Repeated Report activated."
