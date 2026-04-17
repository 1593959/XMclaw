"""
This gene detects when a user repeatedly reports errors (e.g., 'this is broken, please fix error') and triggers a proactive response to address the issue and improve user satisfaction.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class ErrorReporterBehavior(GeneBase):
    gene_id = "gene_e69bad23"
    name = "Error Reporter Behavior"
    description = """This gene detects when a user repeatedly reports errors (e.g., 'this is broken, please fix error') and triggers a proactive response to address the issue and improve user satisfaction."""
    trigger = "The user sends messages containing error-related keywords (e.g., 'broken', 'fix error') more than a defined threshold (e.g., 3 times) within a short time window (e.g., 10 minutes)."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        from datetime import datetime
        
        user_input = context.get("user_input", "")
        now = datetime.now()
        
        # Ensure history storage exists
        if not hasattr(self, "error_history"):
            self.error_history = []
        
        # Keep only entries within the configured time window
        self.error_history = [
            entry for entry in self.error_history
            if (now - entry["timestamp"]).total_seconds() <= self.window_seconds
        ]
        
        # Record the current user message
        self.error_history.append({"timestamp": now, "text": user_input})
        
        # Count how many error-related messages are in the current window
        error_count = sum(
            1 for entry in self.error_history
            if any(kw in entry["text"].lower() for kw in self.error_keywords)
        )
        
        if error_count > self.threshold:
            # Build a high-priority support ticket
            ticket_payload = {
                "priority": "high",
                "user_id": context.get("user_id"),
                "agent_id": context.get("agent_id"),
                "description": f"User reported errors repeatedly: {user_input}",
                "recent_errors": [entry["text"] for entry in self.error_history]
            }
            ticket = await self.create_support_ticket(ticket_payload)
        
            # Alert the support team
            await self.notify_support_team(ticket["id"])
        
            # Offer self-service troubleshooting steps
            response = (
                "It looks like you're encountering some issues. "
                "I've created a high-priority support ticket for you and notified the support team. "
                "While you wait, you might try these steps:\n"
                "1. Refresh the page.\n"
                "2. Clear your browser cache and cookies.\n"
                "3. Check our help center for common issues: https://help.example.com.\n"
                "If the problem persists, please let us know."
            )
        else:
            response = "Thank you for letting us know. We'll look into any issues you may be experiencing."
        
        return response
        return "Gene Error Reporter Behavior activated."