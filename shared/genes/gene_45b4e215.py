"""
Detects when a user reports a bug that has previously been marked as fixed (e.g., message contains 'fix the bug again') and triggers automatic reopening of the original issue, reassigning it to the original developer, adding a comment, and notifying the team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class ReopenFixedBugOnRepeatedReport(GeneBase):
    gene_id = "gene_45b4e215"
    name = "Reopen Fixed Bug on Repeated Report"
    description = """Detects when a user reports a bug that has previously been marked as fixed (e.g., message contains 'fix the bug again') and triggers automatic reopening of the original issue, reassigning it to the original developer, adding a comment, and notifying the team."""
    trigger = "{'type': 'user_report', 'criteria': {'message_contains': 'fix the bug again', 'issue_status': 'resolved'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        issue_status = context.get("issue_status") or (context.get("issue") or {}).get("status")
        issue_id = context.get("issue_id") or (context.get("issue") or {}).get("id")
        original_developer = context.get("original_developer") or "original_developer"
        
        if "fix the bug again" in user_input.lower() and issue_status == "resolved":
            await self.reopen_issue(issue_id)
            await self.assign_issue(issue_id, original_developer)
            await self.add_comment(issue_id, "Issue reopened based on repeat user report.")
            await self.notify_team("slack", "Bug reopened: user reported 'fix the bug again'.")
            return "Issue reopened and team notified."
        else:
            return "No matching bug report to reopen."
        return "Gene Reopen Fixed Bug on Repeated Report activated."