"""
This gene fires when a user reports a broken system and explicitly mentions “error 0”. It acknowledges the issue, creates a high‑priority support ticket, and escalates the incident to the support team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class HandleBroken/error‑0UserReport(GeneBase):
    gene_id = "gene_a63cd72f"
    name = "Handle Broken/Error‑0 User Report"
    description = """This gene fires when a user reports a broken system and explicitly mentions “error 0”. It acknowledges the issue, creates a high‑priority support ticket, and escalates the incident to the support team."""
    trigger = "{'type': 'user_message', 'conditions': {'mustContain': 'error 0', 'anyMatch': ['broken', 'fix']}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract user input from context
                user_input = context.get("user_input", "")
        
                # Optionally validate trigger conditions (the system should already guarantee this)
                if "error 0" not in user_input or not any(kw in user_input for kw in ["broken", "fix"]):
                    return "Trigger conditions not met."
        
                # Step 1: Send acknowledgement to the user
                ack_message = "Sorry to hear you're experiencing a problem. We're looking into the error\u202f0 for you and will get back shortly."
                await self.send_message(ack_message)
        
                # Step 2: Create a high‑priority support ticket
                ticket = await self.create_ticket(
                    title="User reported broken functionality – error\u202f0",
                    priority="high",
                    tags=["broken", "error0"]
                )
                ticket_id = ticket.get("id", "unknown")
        
                # Step 3: Notify the support team about the incident
                notify_message = "User reported broken system with error\u202f0."
                await self.notify_team(team="support", message=notify_message)
        
                # Return a summary of the actions taken
                return f"Acknowledged user, created support ticket #{ticket_id}, and escalated to the support team."
        return "Gene Handle Broken/Error‑0 User Report activated."
