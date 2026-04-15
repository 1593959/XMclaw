"""
When a user reports a bug, automatically create a bug ticket, assign it to the relevant development team, and notify the user of the ticket and expected resolution timeline.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class UserReportedBugFix(GeneBase):
    gene_id = "gene_a60cd681"
    name = "User Reported Bug Fix"
    description = """When a user reports a bug, automatically create a bug ticket, assign it to the relevant development team, and notify the user of the ticket and expected resolution timeline."""
    trigger = "User submits a bug report through the support portal, email, or in‑app feedback channel."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
            agent_id = context.get("agent_id", "unknown")
            timestamp = context.get("timestamp", "unknown")
            channel = context.get("channel", "unknown")
        
            # Extract bug details from user input
            bug_description = user_input
            severity = self._determine_severity(bug_description)
        
            # Create bug ticket in issue-tracking system
            ticket_id = await self._create_ticket(
                title=f"Bug Report: {bug_description[:100]}",
                description=bug_description,
                severity=severity,
                reporter=agent_id,
                channel=channel,
                timestamp=timestamp
            )
        
            # Assign ticket to appropriate development team based on content and severity
            team = self._determine_team(bug_description, severity)
            await self._assign_ticket(ticket_id, team)
        
            # Determine expected resolution timeline based on severity
            timeline = self._get_resolution_timeline(severity)
        
            # Send confirmation message to user
            user_message = (
                f"Thank you for reporting this issue. We have created bug ticket #{ticket_id} "
                f"and assigned it to our {team} team for investigation. "
                f"Based on the severity level ({severity}), we expect to resolve this within {timeline}. "
                f"You can track the progress using ticket #{ticket_id}."
            )
            await self._send_user_notification(agent_id, user_message)
        
            return f"Bug ticket #{ticket_id} created, assigned to {team}, and user notified. Severity: {severity}, Expected resolution: {timeline}."
        return "Gene User Reported Bug Fix activated."
