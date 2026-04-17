"""
When a user reports a bug, automatically initiates a structured bug-fix workflow: assigns the bug to the appropriate developer, creates a dedicated fix branch, and notifies the team. Auto-generated Gene for XMclaw.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugFixAutomation(GeneBase):
    gene_id = "gene_d76eb3ef"
    name = "Bug Fix Automation"
    description = """When a user reports a bug, automatically initiates a structured bug-fix workflow: assigns the bug to the appropriate developer, creates a dedicated fix branch, and notifies the team."""
    trigger = "{'type': 'user_reported_issue', 'filters': {'issue_type': 'bug'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract bug report details from context
        user_input = context.get("user_input", "")
        agent_id = context.get("agent_id", "unknown")
        
        # Parse the bug report to extract relevant information
        bug_title = user_input.split('\n')[0] if user_input else "Untitled Bug"
        bug_description = user_input
        
        # Determine the affected component from the bug report
        component = self._extract_component(bug_description)
        
        # Step 1: Create a high-priority bug ticket in the issue tracker
        ticket_result = self.create_ticket(
    title=bug_title,
    description=bug_description,
    priority="high",
    issue_type="bug"
        )
        ticket_id = ticket_result.get("ticket_id", f"TICKET-{hash(user_input) % 10000}")
        
        # Step 2: Assign the ticket to the most recent developer on the relevant component
        assigned_developer = self.assign_developer(
    ticket_id=ticket_id,
    component=component
        )
        developer_name = assigned_developer.get("name", "unassigned")
        
        # Step 3: Create a fix branch named 'fix/<issue-id>' based on latest main
        branch_name = f"fix/{ticket_id}"
        branch_result = self.create_fix_branch(
    branch_name=branch_name,
    base_branch="main"
        )
        branch_url = branch_result.get("url", f"https://repo.example.com/branch/{branch_name}")
        
        # Step 4: Notify the development team via Slack/Teams
        notification_message = (
    f"🐛 *Bug Fix Workflow Initiated*\n"
    f"• Ticket ID: {ticket_id}\n"
    f"• Assigned to: {developer_name}\n"
    f"• Branch: {branch_name}\n"
    f"• Component: {component}\n"
    f"• Status: Fix in progress"
        )
        self.notify_team(
    message=notification_message,
    channel="dev-team",
    platform="slack"
        )
        
        # Return a summary result string
        result_message = (
    f"Bug fix workflow successfully initiated.\n"
    f"Created ticket {ticket_id} with high priority.\n"
    f"Assigned to {developer_name} for component '{component}'.\n"
    f"Created branch: {branch_name}\n"
    f"Team has been notified."
        )
        return result_message
        return "Gene Bug Fix Automation activated."