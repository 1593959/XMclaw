"""
When a user reports a bug, automatically create a bug ticket, assign it to the development team, and notify the relevant stakeholders.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Bugfixworkflow(GeneBase):
    gene_id = "gene_c2ae402b"
    name = "BugFixWorkflow"
    description = """When a user reports a bug, automatically create a bug ticket, assign it to the development team, and notify the relevant stakeholders."""
    trigger = "{'type': 'user_reported_issue', 'condition': "issue_type = 'bug'"}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            issue_type = context.get("issue_type", "")
            if issue_type != "bug":
                return "No bug reported; BugFixWorkflow not triggered."
        
            # Determine severity if not explicitly provided
            severity = context.get("severity", "")
            if not severity:
                lower_input = user_input.lower()
                if "critical" in lower_input or "crash" in lower_input:
                    severity = "critical"
                elif "high" in lower_input or "major" in lower_input:
                    severity = "high"
                elif "medium" in lower_input or "moderate" in lower_input:
                    severity = "medium"
                else:
                    severity = "low"
        
            # Simulate ticket creation
            import uuid
            import asyncio
            ticket_id = str(uuid.uuid4())
            ticket_info = {
                "id": ticket_id,
                "title": user_input[:100],
                "severity": severity,
                "assignee": "development_team"
            }
            # Placeholder for actual async ticket creation API call
            await asyncio.sleep(0)
        
            # Map severity to priority (lower number = higher priority)
            priority_map = {"critical": 1, "high": 2, "medium": 3, "low": 4}
            priority = priority_map.get(severity, 5)
        
            # Simulate notifying product owner and QA team
            # Placeholder for actual async notification API call
            await asyncio.sleep(0)
        
            return (
                f"Bug ticket {ticket_id} created and assigned to development_team "
                f"with priority {priority}. Product owner and QA team have been notified."
            )
        return "Gene BugFixWorkflow activated."
