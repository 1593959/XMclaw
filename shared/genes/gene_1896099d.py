"""
Detects when a user reports a bug that was previously marked as fixed and triggers remediation steps to ensure the bug is properly re‑evaluated and resolved.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugRecurrenceAuto‑handler(GeneBase):
    gene_id = "gene_1896099d"
    name = "Bug Recurrence Auto‑Handler"
    description = """Detects when a user reports a bug that was previously marked as fixed and triggers remediation steps to ensure the bug is properly re‑evaluated and resolved."""
    trigger = "{'type': 'bug_recurrence', 'condition': 'user_submitted_new_report_matching_existing_fixed_bug', 'context': {'issue_type': 'bug', 'previous_status': 'fixed', 'match_criteria': ['title', 'component', 'severity']}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        bug_data = context.get("bug", {})
        bug_id = bug_data.get("id", "UNKNOWN")
        title = bug_data.get("title", "")
        component = bug_data.get("component", "")
        severity = bug_data.get("severity", "")
        previous_owner = bug_data.get("previous_owner", "")
        
        # Build the notification message using the provided template
        message = f"Bug {bug_id} has been reported again after being marked fixed. Please review, verify the fix, and update the status."
        
        # Notify each required role
        notify_roles = ["developer", "qa_lead", "project_manager"]
        for role in notify_roles:
            await self.send_notification(role, message)
        
        # Reopen the bug so it re‑enters the workflow
        await self.reopen_bug(bug_id)
        
        # Create a subtask for the re‑evaluation work
        subtask_title = f"Re‑evaluate bug {bug_id}: {title}"
        subtask_id = await self.create_subtask(bug_id, subtask_title)
        
        # Assign the bug back to its previous owner if that information is available
        if previous_owner:
            await self.assign_bug(bug_id, previous_owner)
        
        return f"Bug {bug_id} recurrence handled: notified {', '.join(notify_roles)}, reopened bug, created subtask {subtask_id}, assigned to {previous_owner}."
        return "Gene Bug Recurrence Auto‑Handler activated."
