"""
Triggers when a user reports a bug and explicitly asks for it to be fixed again, indicating a recurring or unresolved issue.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class FixBugOnceMore(GeneBase):
    gene_id = "gene_04aa1bb7"
    name = "Fix Bug Once More"
    description = """Triggers when a user reports a bug and explicitly asks for it to be fixed again, indicating a recurring or unresolved issue."""
    trigger = "{'type': 'user_reported_issue', 'issue_type': 'bug', 'request': 'fix', 'reiteration': True}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            agent_id = context.get("agent_id", "unknown")
        
            # Prepare ticket data for bug fix
            ticket_data = {
                "type": "bug_fix",
                "priority": "high",
                "assign_to": "development_team",
                "user_input": user_input,
                "reiteration": True
            }
        
            # Create the automated ticket (simulating async operation)
            try:
                await self.ticket_service.create(ticket_data)
                ticket_created = True
            except Exception as e:
                ticket_created = False
                error_msg = str(e)
        
            # Prepare notification message
            message = "We have received your request to fix the bug again. Our team will prioritize the fix and keep you updated."
        
            # Notify user if ticket was created successfully
            if ticket_created:
                await self.notification_service.send_user_message(
                    user_id=context.get("user_id"),
                    message=message
                )
                return f"Bug fix ticket created with high priority and assigned to development team. User has been notified."
            else:
                return f"Failed to create bug fix ticket. Error: {error_msg}"
        return "Gene Fix Bug Once More activated."
