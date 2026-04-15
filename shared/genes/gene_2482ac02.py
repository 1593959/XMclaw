"""
This gene activates when a user explicitly requests that a bug be fixed again. It triggers a fresh bug‑fix workflow that re‑creates or re‑opens a bug ticket, notifies the responsible developer, re‑runs the related CI tests, and logs the retry attempt.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class RetryBugFix(GeneBase):
    gene_id = "gene_2482ac02"
    name = "Retry Bug Fix"
    description = """This gene activates when a user explicitly requests that a bug be fixed again. It triggers a fresh bug‑fix workflow that re‑creates or re‑opens a bug ticket, notifies the responsible developer, re‑runs the related CI tests, and logs the retry attempt."""
    trigger = "{'type': 'user_intent', 'condition': 'user input matches the pattern "fix the bug once more" (case‑insensitive) or contains phrases like "fix again", "re‑fix bug", "fix it again".', 'examples': ['fix the bug once more', 'Please fix the bug again', 'Do another fix for the issue']}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            user_input_lower = user_input.lower()
        
            # Define trigger phrases (case‑insensitive)
            trigger_phrases = [
                "fix the bug once more",
                "fix again",
                "re-fix bug",
                "fix it again"
            ]
        
            if not any(phrase in user_input_lower for phrase in trigger_phrases):
                return "No retry trigger detected."
        
            # Extract identifiers from context
            bug_id = context.get("bug_id", "UNKNOWN")
            user_id = context.get("user_id", "UNKNOWN")
            agent_id = context.get("agent_id", "UNKNOWN")
        
            # Step 1: create or reopen a bug ticket
            ticket_status = await self.create_or_reopen_ticket(bug_id)
        
            # Step 2: notify the responsible developer
            notify_status = await self.notify_developer(bug_id, agent_id)
        
            # Step 3: retrigger CI/CD pipeline for the bug‑fix test suite
            ci_status = await self.retrigger_ci(bug_id)
        
            # Step 4: log the retry attempt
            from datetime import datetime, timezone
            log_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "bug_id": bug_id,
                "agent_id": agent_id,
                "ticket_status": ticket_status,
                "notify_status": notify_status,
                "ci_status": ci_status
            }
            await self.log_attempt(log_record)
        
            return (
                f"Retry workflow executed for bug {bug_id}. "
                f"Ticket: {ticket_status}. "
                f"Notification: {notify_status}. "
                f"CI: {ci_status}."
            )
        return "Gene Retry Bug Fix activated."
