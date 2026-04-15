"""
This gene is activated when a user reports a bug. It triggers an automated workflow to create a bug‑fix task, assign it to the appropriate developer, and notify the team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugFixRequestGene(GeneBase):
    gene_id = "gene_41a225df"
    name = "Bug Fix Request Gene"
    description = """This gene is activated when a user reports a bug. It triggers an automated workflow to create a bug‑fix task, assign it to the appropriate developer, and notify the team."""
    trigger = "{'type': 'user_report', 'filters': {'issue_type': 'bug'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        reporter = context.get("user_id", "unknown")
        # Build a title for the bug report – use explicit title if present, otherwise truncate the input
        title = context.get("title") or ("Bug Report: " + user_input[:50] + "...")
        description = user_input
        priority = context.get("priority", "medium")
        
        # Create the bug‑fix task in the issue tracking system
        task = await self.create_task(title=title, description=description, priority=priority)
        task_id = task.get("id", "unknown")
        
        # Determine the developer to assign – prefer explicit assignment, fall back to a default
        developer_id = context.get("developer_id") or context.get("assigned_developer") or "unassigned"
        assign_result = await self.assign_developer(task_id, developer_id)
        
        # Notify the team about the new bug report
        team_recipients = context.get("team_recipients", ["team@example.com"])
        notification_message = (
            f"New bug report created: '{title}' (Task ID: {task_id}). "
            f"Assigned to developer: {developer_id}. "
            f"Reporter: {reporter}."
        )
        await self.notify_team(recipients=team_recipients, message=notification_message)
        
        return f"Bug‑fix task {task_id} created, assigned to {developer_id}, and team notified."
        return "Gene Bug Fix Request Gene activated."
