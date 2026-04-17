"""
Detects when a user reports that an issue is still broken after a fix has been applied, and automatically triggers an escalation workflow to ensure the problem is addressed promptly.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BrokenStillIssueDetectionAndEscalation(GeneBase):
    gene_id = "gene_ff9b75c3"
    name = "Broken Still Issue Detection and Escalation"
    description = """Detects when a user reports that an issue is still broken after a fix has been applied, and automatically triggers an escalation workflow to ensure the problem is addressed promptly."""
    trigger = "{'type': 'user_feedback', 'condition': {'contains': ['broken still', 'still broken', 'issue persists', 'still not working']}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "").lower()
        trigger_phrases = ['broken still', 'still broken', 'issue persists', 'still not working']
        
        if any(phrase in user_input for phrase in trigger_phrases):
            user_id = context.get("user_id", "unknown")
            user_input_original = context.get("user_input", "")
            timestamp = context.get("timestamp")
        
            # Step 1: Log the incident with timestamp, user ID, and exact phrasing
            await self.log_incident(
                user_id=user_id,
                timestamp=timestamp,
                feedback=user_input_original
            )
        
            # Step 2: Create high priority ticket linked to original issue
            original_issue_id = context.get("original_issue_id")
            ticket = await self.create_high_priority_ticket(
                description=f"Issue persists after fix reported: {user_input_original}",
                linked_issue_id=original_issue_id
            )
        
            # Step 3: Notify development team with summary
            ticket_id = ticket.get("id") if ticket else None
            await self.notify_development_team(
                ticket_id=ticket_id,
                summary=f"User {user_id} reports issue is still broken: {user_input_original}"
            )
        
            # Step 4: Send acknowledgment to user with expected timeline
            await self.send_acknowledgment_to_user(
                user_id=user_id,
                message="Thank you for reporting. We have confirmed your feedback and escalated this to our development team as high priority. We are actively working on resolving this issue and will update you once it is fixed."
            )
        
            return "Escalation workflow initiated. High-priority ticket created and development team notified."
        return "No trigger detected."
        return "Gene Broken Still Issue Detection and Escalation activated."