"""Auto-generated Gene for XMclaw.
When a user reports a bug (e.g., via the support portal or feedback form), automatically create a bug ticket in the internal issue-tracking system, assign it to the development team, and send a confirmation notification to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Autobugticketcreation(GeneBase):
    gene_id = "gene_3ee5be7e"
    name = "AutoBugTicketCreation"
    description = "When a user reports a bug (e.g., via the support portal or feedback form), automatically create a bug ticket in the internal issue-tracking system, assign it to the development team, and send a confir"
    trigger = "{'type': 'user_event', 'event': 'bug_report', 'conditions': [{'field': 'source', 'operator': 'in', 'value': ['support_portal', 'feedback_form', 'in_app']}]}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene AutoBugTicketCreation activated."
