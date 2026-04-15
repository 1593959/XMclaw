"""
When a user reports a bug (issue with label “bug”), automatically create a dedicated fix branch, assign the issue to the appropriate developer, and notify the team so the bug can be addressed promptly.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class AutoBugFixWorkflow(GeneBase):
    gene_id = "gene_b2c68c50"
    name = "Auto Bug Fix Workflow"
    description = """When a user reports a bug (issue with label “bug”), automatically create a dedicated fix branch, assign the issue to the appropriate developer, and notify the team so the bug can be addressed promptly."""
    trigger = "{'type': 'issue_created', 'conditions': {'labels': {'contains': 'bug'}}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        import re
        
            # Retrieve the issue details from the context
            issue = context.get("issue", {})
            if not issue:
                return "No issue found in context. Workflow aborted."
        
            issue_id = issue.get("id")
            title = issue.get("title", "")
            labels = issue.get("labels", [])
        
            # Ensure the bug label is present (trigger already guarantees this)
            if "bug" not in labels:
                return "Issue does not have the 'bug' label. Skipping auto‑fix workflow."
        
            # Slugify the title to create a safe branch name segment
            slug = re.sub(r'[^a-z0-9\s-]', '', title.lower())
            slug = re.sub(r'[\s_]+', '-', slug).strip('-')
            if not slug:
                slug = "untitled"
        
            branch_name = f"fix/{issue_id}-{slug}"
        
            # 1️⃣ Create the dedicated fix branch
            try:
                # Assuming self.git provides an async method to create branches
                await self.git.create_branch(
                    branch_name,
                    base_branch=issue.get("default_branch", "main")
                )
            except Exception as e:
                return f"Failed to create branch '{branch_name}': {e}"
        
            # 2️⃣ Assign the issue to the appropriate developer
            try:
                # self.issue_tracker.auto_assign_by_component resolves the assignee based on component labels
                assignee = await self.issue_tracker.auto_assign_by_component(issue_id)
            except Exception as e:
                assignee = "unassigned"
                # Log the error for later inspection (optional)
        
            # 3️⃣ Notify the team about the bug and the created fix branch
            notification_message = (
                f"Bug reported: {title} (ID: {issue_id}). "
                f"Fix branch created and issue assigned to {assignee}."
            )
            try:
                # self.notification.send_async sends a message to a given channel
                await self.notification.send(
                    channel="#dev-alerts",
                    message=notification_message
                )
            except Exception as e:
                # If notification fails we still return success for branch/assign steps
                return (
                    f"Branch '{branch_name}' created and issue {issue_id} assigned to {assignee}, "
                    f"but notification failed: {e}"
                )
        
            # Return a concise summary of actions taken
            return (
                f"Auto‑fix workflow completed: "
                f"Branch '{branch_name}' created, issue {issue_id} assigned to {assignee}, "
                f"team notified in #dev-alerts."
            )
        return "Gene Auto Bug Fix Workflow activated."
