"""
When a user reports a bug (e.g., via the support portal or feedback form), automatically create a bug ticket in the internal issue‑tracking system, assign it to the development team, and send a confirmation notification to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Autobugticketcreation(GeneBase):
    gene_id = "gene_3ee5be7e"
    name = "AutoBugTicketCreation"
    description = """When a user reports a bug (e.g., via the support portal or feedback form), automatically create a bug ticket in the internal issue‑tracking system, assign it to the development team, and send a confirmation notification to the user."""
    trigger = "{'type': 'user_event', 'event': 'bug_report', 'conditions': [{'field': 'source', 'operator': 'in', 'value': ['support_portal', 'feedback_form', 'in_app']}]}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Validate that the incoming event matches the bug‑report trigger
            if context.get("event") != "bug_report":
                return "No bug report detected; no action taken."
            source = context.get("source", "")
            if source not in ("support_portal", "feedback_form", "in_app"):
                return "Bug report source is not eligible for auto‑ticket creation."
        
            # Assemble the ticket payload according to the gene’s action spec
            ticket_payload = {
                "template": "bug_ticket",
                "assignee": "development_team",
                "priority": "high",
                "tags": ["user_reported", "auto-created"],
                "description": context.get("user_input", ""),
                "reporter": context.get("user_id", "unknown")
            }
        
            # Create the ticket in the internal issue‑tracking system
            try:
                ticket = await self.create_ticket(
                    target_system="issue_tracker",
                    **ticket_payload
                )
            except Exception as e:
                return f"Failed to create bug ticket: {e}"
        
            # Notify the user if the gene is configured to do so
            if context.get("notify_user", True):
                channels = context.get("notify_channels", ["email", "slack"])
                notification_message = (
                    f"Your bug report has been received and a ticket "
                    f"{ticket.get('id', 'N/A')} has been created. "
                    f"Our development team will review it shortly."
                )
                try:
                    await self.notify_user(
                        user_id=context.get("user_id", "unknown"),
                        channels=channels,
                        message=notification_message
                    )
                except Exception as e:
                    return (
                        f"Bug ticket {ticket.get('id')} created, "
                        f"but failed to notify user: {e}"
                    )
        
            return f"Bug ticket {ticket.get('id')} created and user notified."
        return "Gene AutoBugTicketCreation activated."
