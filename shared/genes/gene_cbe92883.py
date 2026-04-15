"""
When a user reports the same bug again, automatically create or reopen a bug ticket, assign it to the appropriate developer, notify the team, and trigger an automated fix pipeline.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class AutoBugFix(re‑reportedIssue)(GeneBase):
    gene_id = "gene_cbe92883"
    name = "Auto Bug Fix (Re‑reported Issue)"
    description = """When a user reports the same bug again, automatically create or reopen a bug ticket, assign it to the appropriate developer, notify the team, and trigger an automated fix pipeline."""
    trigger = "{'type': 'user_report', 'event': 'bug_reopened', 'conditions': {'reporter': 'any', 'duplicate_check': True}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        bug_id = context.get("bug_id")
        
        # Step 1 – Create or reopen the bug ticket (reuse existing if possible)
        ticket = await create_or_reopen_ticket(
            bug_id=bug_id,
            description=user_input,
            policy="use_existing_if_exists"
        )
        
        # Step 2 – Assign the ticket to the least‑loaded developer
        developer = await assign_developer(
            ticket_id=ticket["id"],
            policy="least_loaded"
        )
        
        # Step 3 – Notify the team through Slack and email
        await notify_team(
            ticket_id=ticket["id"],
            developer=developer,
            channels=["slack", "email"]
        )
        
        # Step 4 – Trigger the automated fix pipeline for this reopen
        await run_fix_pipeline(
            pipeline="auto_fix_on_reopen",
            ticket_id=ticket["id"]
        )
        
        # Return a concise summary of the actions performed
        action = "reopened" if ticket.get("was_existing") else "created"
        return f"Bug ticket {ticket['id']} {action}, assigned to {developer['name']}, team notified, auto‑fix pipeline triggered."
        return "Gene Auto Bug Fix (Re‑reported Issue) activated."
