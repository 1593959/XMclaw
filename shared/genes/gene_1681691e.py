"""
Detects when a user encounters the same error more than once within a short time window and automatically triggers a supportive action to reduce frustration (e.g., creating a support ticket and surfacing a relevant help article).
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class RepeatedErrorDetection_Response(GeneBase):
    gene_id = "gene_1681691e"
    name = "Repeated Error Detection & Response"
    description = """Detects when a user encounters the same error more than once within a short time window and automatically triggers a supportive action to reduce frustration (e.g., creating a support ticket and surfacing a relevant help article)."""
    trigger = "{'type': 'error_occurrence', 'condition': 'count >= 2', 'timeWindow': '5m', 'errorCode': 'any', 'context': {'userId': '<user_id>', 'sessionId': '<session_id>'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract error details from context
        error_code = context.get('error_code')
        error_message = context.get('error_message', '')
        user_id = context.get('user_id')
        session_id = context.get('session_id')
        current_time = time.time()
        
        # Ensure error history storage exists on the instance
        if not hasattr(self, '_error_history'):
            self._error_history = {}
        
        # Use a composite key for user, session and error code
        key = (user_id, session_id, error_code)
        history = self._error_history.get(key, [])
        history.append(current_time)
        
        # Keep only occurrences within the last 5 minutes (300 seconds)
        window_seconds = 300
        recent = [t for t in history if current_time - t <= window_seconds]
        self._error_history[key] = recent
        
        # Detect repeated error (count >= 2 within the window)
        if len(recent) >= 2:
            # Create high-priority support ticket
            ticket = await self.support_service.create_ticket(
                user_id=user_id,
                session_id=session_id,
                description=f"Repeated error '{error_code}': {error_message}",
                priority='high'
            )
            # Notify the support team about the new ticket
            await self.support_service.notify_team(
                ticket_id=ticket.id,
                message='User encountered the same error multiple times within 5 minutes.'
            )
            # Surface the relevant help article to the user
            await self.help_article_service.show_article(
                user_id=user_id,
                article_id='help_article_error_repeat'
            )
            return (
                f"Repeated error '{error_code}' detected. "
                f"Support ticket {ticket.id} created with high priority, "
                f"support team notified, and help article shown."
            )
        
        # No repeat detected - log the error and exit quietly
        return "Error logged."
        return "Gene Repeated Error Detection & Response activated."