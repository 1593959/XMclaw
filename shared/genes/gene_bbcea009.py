"""
Automatically creates a bug ticket and notifies the development team when a user reports a bug.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugReportHandlingGene(GeneBase):
    gene_id = "gene_bbcea009"
    name = "Bug Report Handling Gene"
    description = """Automatically creates a bug ticket and notifies the development team when a user reports a bug."""
    trigger = "{'type': 'user_reported_issue', 'filters': {'category': 'bug'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Ensure this gene only handles bug reports
            trigger = context.get("trigger", {})
            if trigger.get("type") != "user_reported_issue" or trigger.get("filters", {}).get("category") != "bug":
                return "No bug handling needed."
        
            user_input = context.get("user_input", "")
            if not user_input:
                return "No bug description provided."
        
            # Build ticket payload
            ticket_payload = {
                "project": "default",
                "type": "bug",
                "title": f"Bug Report: {user_input[:80]}",
                "description": user_input,
                "assignee": "development_team",
                "priority": "high",
                "reporter": context.get("user_id", "unknown"),
                "agent_id": context.get("agent_id")
            }
        
            # Create the bug ticket
            try:
                ticket_id = await create_ticket(ticket_payload)
            except Exception as e:
                return f"Failed to create bug ticket: {str(e)}"
        
            # Notify the development team
            notify_channels = ["email", "slack"]
            notification_message = (
                f"New high‑priority bug ticket created: [{ticket_id}] "
                f"Title: {ticket_payload['title']}. "
                f"Please prioritize fixing."
            )
        
            try:
                await notify_team(notify_channels, notification_message)
            except Exception as e:
                return f"Bug ticket {ticket_id} created, but notification failed: {str(e)}"
        
            return f"Bug ticket {ticket_id} created and team notified via {', '.join(notify_channels)}."
        return "Gene Bug Report Handling Gene activated."
