"""
This gene activates when a user reports that a bug fix is incorrect, initiating a reassessment and re-opening the bug for the development team. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class HandleWrongBugFixReport(GeneBase):
    gene_id = "gene_b0983f19"
    name = "Handle Wrong Bug Fix Report"
    description = """This gene activates when a user reports that a bug fix is incorrect, initiating a reassessment and re-opening the bug for the development team."""
    trigger = "{'type': 'user_feedback', 'filter': {'feedback_type': 'wrong_fix', 'source': 'bug_fix_page'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        bug_id = context.get("bug_id", "unknown")
        user_input = context.get("user_input", "")
        
        # Log the user feedback
        self.log(f"User reported wrong fix for bug {bug_id}")
        
        # Create a high-priority ticket in BugTracker
        ticket_data = {
    "title": f"Wrong fix reported for bug {bug_id}",
    "project": "BugTracker",
    "labels": ["wrong-fix"],
    "priority": "high"
        }
        self.create_ticket(ticket_data)
        
        # Notify the dev and QA teams via Slack
        self.notify(
    recipients=["dev_team", "qa_team"],
    channel="slack",
    template=f"Wrong fix reported for bug {bug_id}. Please review and reopen."
        )
        
        # Update the bug status to reopened
        self.update_bug_status(bug_id=bug_id, status="reopened")
        
        return f"Successfully processed wrong fix report for bug {bug_id}. Bug has been reopened and development team notified."
        return "Gene Handle Wrong Bug Fix Report activated."