"""
When a user reports a bug that was previously marked as fixed, this rule automatically reopens the ticket, reassigns it to the original developer, and notifies the team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class ReopenFixedBugOnRepeatedReport(GeneBase):
    gene_id = "gene_8b76ddb6"
    name = "Reopen Fixed Bug on Repeated Report"
    description = """When a user reports a bug that was previously marked as fixed, this rule automatically reopens the ticket, reassigns it to the original developer, and notifies the team."""
    trigger = "{'type': 'bug_report', 'conditions': [{'field': 'ticket.status', 'operator': 'equals', 'value': 'Closed'}, {'field': 'ticket.resolution', 'operator': 'equals', 'value': 'Fixed'}, {'field': 'ticket.closed_at', 'operator': 'within_last', 'value': '30 days'}]}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        ticket = context.get("ticket", {})
            user = context.get("user", {})
        
            # Verify trigger conditions
            if (ticket.get("status") == "Closed" and 
                ticket.get("resolution") == "Fixed"):
        
                closed_at = ticket.get("closed_at")
                if closed_at:
                    # Check if closed within last 30 days
                    from datetime import datetime, timedelta
                    if isinstance(closed_at, str):
                        closed_at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        
                    if datetime.now(closed_at.tzinfo) - closed_at <= timedelta(days=30):
                        ticket_id = ticket.get("id")
                        original_assignee = ticket.get("original_assignee")
        
                        # Execute actions
                        await self.reopen_ticket(ticket_id)
                        await self.assign_ticket(ticket_id, original_assignee)
                        await self.notify(
                            channel="dev-alerts",
                            message=f"Bug {ticket_id} reported again by {user.get('name')}."
                        )
                        await self.add_comment(
                            ticket_id=ticket_id,
                            comment="User reported this bug again. Reopening ticket."
                        )
        
                        return f"Bug {ticket_id} has been reopened and assigned to {original_assignee}."
        
            return "No action taken."
        return "Gene Reopen Fixed Bug on Repeated Report activated."
