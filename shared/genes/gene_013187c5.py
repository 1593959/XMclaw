"""
Detects when a user submits a bug report that indicates a previously-fixed bug has re-occurred (e.g., the user says "fix the bug again") and automatically routes the issue for quick resolution, assigns it to the original developer, tags it as a repeat-bug, alerts the team, and creates a high-priority investigation sub-task.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class HandleRepeatedBugReports(GeneBase):
    gene_id = "gene_013187c5"
    name = "Handle Repeated Bug Reports"
    description = """Detects when a user submits a bug report that indicates a previously-fixed bug has re-occurred (e.g., the user says "fix the bug again") and automatically routes the issue for quick resolution, assigns it to the original developer, tags it as a repeat-bug, alerts the team, and creates a high-priority investigation sub-task."""
    trigger = "{'type': 'event', 'source': 'issue_tracker', 'eventName': 'issue.created', 'filter': {'and': [{'field': 'type', 'operator': 'equals', 'value': 'bug'}, {'field': 'body', 'operator': 'contains', 'value': 'fix the bug again'}]}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract issue details from context
        issue = context.get("issue", {})
        issue_id = issue.get("id", "unknown")
        issue_title = issue.get("title", "Untitled")
        issue_body = issue.get("body", "")
        
        # Verify this is a repeat bug report
        if "fix the bug again" not in issue_body.lower():
            return "No repeat bug detected."
        
        # Get the original assignee from the issue metadata or context
        original_assignee = issue.get("assignee") or issue.get("original_assignee") or context.get("original_assignee", "unassigned")
        
        # Initialize result tracking
        actions_taken = []
        
        # Assign the issue to the original developer
        if original_assignee and original_assignee != "unassigned":
            if hasattr(self, 'issue_tracker') and self.issue_tracker:
                await self.issue_tracker.assign_issue(issue_id, original_assignee)
            actions_taken.append(f"assigned to {original_assignee}")
        else:
            actions_taken.append("assigned to unassigned (original developer not found)")
        
        # Add repeat-bug and high-priority labels
        labels = ['repeat-bug', 'high-priority']
        if hasattr(self, 'issue_tracker') and self.issue_tracker:
            await self.issue_tracker.add_labels(issue_id, labels)
        actions_taken.append(f"labeled as {', '.join(labels)}")
        
        # Send Slack notification to the bugs channel
        slack_message = f"Repeat bug reported: {issue_title}. Assigned to {original_assignee}."
        if hasattr(self, 'notification_service') and self.notification_service:
            await self.notification_service.send_message("slack-bugs", slack_message)
        actions_taken.append("notified team via Slack")
        
        # Create high-priority investigation subtask
        subtask_title = "Investigate repeat bug"
        subtask_description = "Confirm the bug reoccurrence, verify the previous fix, and apply any necessary corrections."
        if hasattr(self, 'issue_tracker') and self.issue_tracker:
            await self.issue_tracker.create_subtask(
                issue_id,
                title=subtask_title,
                description=subtask_description,
                priority="high"
            )
        actions_taken.append("created high-priority investigation subtask")
        
        # Compile and return result
        result = f"Processed repeat bug report ({issue_id}): {'; '.join(actions_taken)}."
        return result
        return "Gene Handle Repeated Bug Reports activated."