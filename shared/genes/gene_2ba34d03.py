"""
Skill that automatically detects error code 4 reported by a user, retrieves relevant logs, creates a support ticket, attempts remediation, and notifies the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Autofixerror4(GeneBase):
    gene_id = "gene_2ba34d03"
    name = "AutoFixError4"
    description = """Skill that automatically detects error code 4 reported by a user, retrieves relevant logs, creates a support ticket, attempts remediation, and notifies the user."""
    trigger = "User says 'this is broken, please fix error 4'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_info = context.get_current_error()
        if error_info.code == 4:
            # Retrieve logs for the last hour
            logs = self.log_service.fetch_logs(since=datetime.now() - timedelta(hours=1))
            # Create a support ticket
            ticket = self.support.create_ticket(
                title=f"Auto-fix Error 4: {error_info.message}",
                description=f"Error details:\n{error_info.details}\n\nRecent logs:\n{logs}"
            )
            # Attempt automatic remediation
            self.remediation.apply_fix('error_4', context)
            # Notify the user
            context.respond(f"We have detected error 4 and are working on it. Ticket ID: {ticket.id}")
        else:
            context.respond("No automatic action for this error.")
        return "Gene AutoFixError4 activated."