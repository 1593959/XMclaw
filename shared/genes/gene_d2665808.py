"""
Automatically creates a bug ticket, assigns it to the relevant team, and initiates a fix workflow whenever a user reports a bug.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Autobugfix(GeneBase):
    gene_id = "gene_d2665808"
    name = "AutoBugFix"
    description = """Automatically creates a bug ticket, assigns it to the relevant team, and initiates a fix workflow whenever a user reports a bug."""
    trigger = "{'type': 'user_report', 'source': ['feedback_form', 'support_ticket', 'in_app_bug_button'], 'condition': 'user submits a bug report'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        component = context.get("component", "general")
        priority = "high"
        labels = ["bug", "user-reported"]
        
        # Step 1 – create the bug ticket
        ticket = await ticket_service.create_ticket(
            title="Bug reported by user",
            description=user_input,
            priority=priority,
            labels=labels
        )
        
        # Step 2 – determine the responsible team based on the affected component
        team_map = {
            "frontend": "Frontend Team",
            "backend": "Backend Team",
            "database": "Database Team",
            "general": "General Team"
        }
        team = team_map.get(component, "General Team")
        
        # Auto‑select an assignee with matching expertise
        assignee = await team_assignment_engine.get_assignee(team=team, expertise=component)
        
        # Step 3 – assign the ticket to the selected team and assignee
        await team_assignment_engine.assign_ticket(team=team, assignee=assignee)
        
        # Step 4 – notify the developer (Slack) about the new bug
        await notification_service.notify_developer(
            channel="slack",
            message=f"New bug reported – please start investigation. Ticket ID: {ticket.id}"
        )
        
        # Step 5 – trigger the bug‑fix CI pipeline workflow
        await ci_pipeline.start_fix_workflow(
            workflow="bug_fix",
            trigger="on_assigned"
        )
        
        return f"Bug ticket {ticket.id} created, assigned to {assignee} on {team}, and fix workflow initiated."
        return "Gene AutoBugFix activated."
