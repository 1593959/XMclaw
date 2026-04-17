"""
This gene activates when a user reports a bug and explicitly asks for it to be fixed again. It automatically re-opens the corresponding bug ticket, escalates its priority, and alerts the responsible development team.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class FixBugOnceMore(GeneBase):
    gene_id = "gene_a3caf862"
    name = "Fix Bug Once More"
    description = """This gene activates when a user reports a bug and explicitly asks for it to be fixed again. It automatically re-opens the corresponding bug ticket, escalates its priority, and alerts the responsible development team."""
    trigger = "{'type': 'user_report', 'pattern': 'fix the bug once more'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        if "fix the bug once more" not in user_input.lower():
            return "Trigger pattern not found."
        
        bug_id = context.get("bug_id") or context.get("ticket_id")
        if not bug_id:
            return "No bug identifier found in context."
        
        try:
            await self.reopen_bug_ticket(
                bug_id=bug_id,
                escalate_priority=True,
                notify_team=True,
                assign_to="development_team"
            )
            return f"Bug ticket {bug_id} reopened, priority escalated, and development team alerted."
        except Exception as e:
            return f"Failed to reopen bug ticket {bug_id}: {e}"
        return "Gene Fix Bug Once More activated."