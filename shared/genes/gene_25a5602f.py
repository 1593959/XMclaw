"""Auto-generated Gene for XMclaw.
When a user reports a bug (e.g., via the support portal), automatically create a bug ticket, assign it to the development team with status 'Open' and priority 'Medium', and send an acknowledgement email to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class Fixbugonuserreport(GeneBase):
    gene_id = "gene_25a5602f"
    name = "FixBugOnUserReport"
    description = "When a user reports a bug (e.g., via the support portal), automatically create a bug ticket, assign it to the development team with status 'Open' and priority 'Medium', and send an acknowledgement ema"
    trigger = "{'type': 'user_report', 'event': 'bug_report', 'source': 'support_portal'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene FixBugOnUserReport activated."
