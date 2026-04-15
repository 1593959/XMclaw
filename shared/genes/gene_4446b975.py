"""
When a user reports a bug (e.g., by saying 'fix the bug'), this gene automatically creates a bug ticket, assigns it to the appropriate development team, and notifies the user of the ticket number.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Bugfixtrigger(GeneBase):
    gene_id = "gene_4446b975"
    name = "BugFixTrigger"
    description = """When a user reports a bug (e.g., by saying 'fix the bug'), this gene automatically creates a bug ticket, assigns it to the appropriate development team, and notifies the user of the ticket number."""
    trigger = "{'type': 'UserInput', 'condition': "User message contains 'fix the bug'", 'source': 'any'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        if "fix the bug" in user_input.lower():
            issue = await self.create_issue(
                title="Bug Fix Request",
                issue_type="Bug",
                project="DefaultProject",
                assignee="DevelopmentTeam",
                notify_user=True,
                labels=["user-reported"]
            )
            ticket_id = issue.get("id", "Unknown")
            return f"Bug ticket #{ticket_id} created and assigned to DevelopmentTeam. You will be notified of any updates."
        return ""
        return "Gene BugFixTrigger activated."
