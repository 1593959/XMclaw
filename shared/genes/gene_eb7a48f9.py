"""
When a user reports a broken feature and mentions “error 1”, automatically create a high‑priority bug ticket, assign it to the development team, and notify the team via Slack so the issue can be diagnosed and resolved promptly.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class FixUser‑reportedError1(GeneBase):
    gene_id = "gene_eb7a48f9"
    name = "Fix User‑Reported Error 1"
    description = """When a user reports a broken feature and mentions “error 1”, automatically create a high‑priority bug ticket, assign it to the development team, and notify the team via Slack so the issue can be diagnosed and resolved promptly."""
    trigger = "{'type': 'user_report', 'keywords': ['broken', 'error 1'], 'severity': 'high'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            # Verify the trigger conditions
            if (
                context.get("type") == "user_report"
                and context.get("severity") == "high"
                and "broken" in user_input.lower()
                and "error 1" in user_input.lower()
            ):
                # Create a high‑priority bug ticket assigned to the development team
                ticket = await self.create_ticket(
                    ticket_type="bug",
                    priority="high",
                    assignee="development_team",
                    description=f"User reported broken feature with error 1: {user_input}"
                )
                # Notify the team via Slack
                await self.notify_slack(
                    channel="dev-alerts",
                    message=f"High‑priority bug ticket created: {ticket.get('id')}. Please investigate."
                )
                return f"Bug ticket {ticket.get('id')} created and dev‑alerts notified."
            return "No relevant error reported."
        return "Gene Fix User‑Reported Error 1 activated."
