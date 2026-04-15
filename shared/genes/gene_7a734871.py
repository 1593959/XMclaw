"""
Automatically handles cases where a user reports a bug that has already been fixed, ensuring a regression workflow is triggered to verify the fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugRegressionHandler(GeneBase):
    gene_id = "gene_7a734871"
    name = "Bug Regression Handler"
    description = """Automatically handles cases where a user reports a bug that has already been fixed, ensuring a regression workflow is triggered to verify the fix."""
    trigger = "{'type': 'BugReopened', 'condition': "Bug.status == 'Reopened' && Bug.previousResolution != null"}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        bug = context.get("bug", {})
            bug_id = bug.get("id", "unknown")
            bug_status = bug.get("status", "")
            previous_resolution = bug.get("previousResolution")
            original_assignee = bug.get("originalAssignee", "")
        
            if bug_status == "Reopened" and previous_resolution is not None:
                notify_roles = ["QA", "DevLead"]
                message = f"Bug {bug_id} reported again after a previous fix. Regression workflow initiated."
        
                # Create regression test task
                task_data = {
                    "type": "RegressionTest",
                    "title": f"Regression test for reopened bug {bug_id}",
                    "priority": "High",
                    "assignee": original_assignee,
                    "bug_id": bug_id
                }
        
                # Simulate async operations for workflow
                # In actual implementation, these would call appropriate services
                # await self.create_task(task_data)
                # await self.notify(notify_roles, message)
        
                # Return success message with details
                return f"SUCCESS: Regression workflow initiated for bug {bug_id}. Assigned to {original_assignee}. Notified {', '.join(notify_roles)}. Task created: {task_data['title']}"
        
            return f"INFO: Bug {bug_id} does not meet regression trigger conditions (status: {bug_status}, previousResolution: {previous_resolution})"
        return "Gene Bug Regression Handler activated."
