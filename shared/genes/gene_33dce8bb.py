"""
Triggers when a user reports that a previously resolved bug is still not fixed. This gene re-opens the bug ticket, assigns it back to the original developer for a new fix, and notifies the QA team to verify the resolution.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugFixReVerification(GeneBase):
    gene_id = "gene_33dce8bb"
    name = "Bug Fix Re-Verification"
    description = """Triggers when a user reports that a previously resolved bug is still not fixed. This gene re-opens the bug ticket, assigns it back to the original developer for a new fix, and notifies the QA team to verify the resolution."""
    trigger = "{'type': 'event', 'event': 'bug_reopened', 'conditions': [{'field': 'previous_status', 'operator': 'equals', 'value': 'resolved'}, {'field': 'reporter_type', 'operator': 'equals', 'value': 'user'}]}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        bug_id = context.get("bug_id") or (context.get("event") or {}).get("bug_id")
        original_developer = context.get("original_developer") or (context.get("event") or {}).get("original_developer")
        
        if not bug_id:
            return "Error: bug_id not found in context."
        
        # Verify that the bug was previously resolved and reported by a user
        prev_status = (context.get("event") or {}).get("previous_status")
        reporter_type = (context.get("event") or {}).get("reporter_type")
        if prev_status != "resolved":
            return f"Skipped: bug was not previously resolved (status={prev_status})."
        if reporter_type != "user":
            return f"Skipped: reporter is not a user (type={reporter_type})."
        
        # Reopen the bug ticket
        await self.ticket_service.reopen(bug_id)
        
        # Assign the bug back to the original developer with a note
        assign_note = f"Bug {bug_id} reported again. Please verify and fix."
        await self.ticket_service.assign(bug_id, original_developer, assign_note)
        
        # Notify QA team and product owner
        notify_message = f"Bug {bug_id} has been reopened. Please verify the fix."
        await self.notification_service.send(
            recipients=["qa_team", "product_owner"],
            message=notify_message
        )
        
        return f"Bug {bug_id} reopened, assigned to {original_developer}, and QA team plus product owner notified."
        return "Gene Bug Fix Re-Verification activated."