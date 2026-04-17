"""
Skill that automatically handles user reports of broken functionality mentioning error 3: logs the issue, runs diagnostics, attempts a fix, and escalates if necessary.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_77cb7394"
    name = "FixError3Skill"
    description = """Skill that automatically handles user reports of broken functionality mentioning error 3: logs the issue, runs diagnostics, attempts a fix, and escalates if necessary."""
    trigger = "{'type': 'keyword', 'keywords': ['error 3', 'broken']}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Extract error identifier
        error_id = context.get("error_id", "3")
        # Log the reported issue
        log.error(f"User reported error {error_id}: {context.get('user_message')}")
        # Run diagnostic checks
        diag_result = diagnostics.run_check(error_id)
        if diag_result.success:
            # Apply fix if possible
            fix.apply(error_id)
            response = f"Error {error_id} has been resolved."
        else:
            # Create support ticket
            ticket_id = support.create_ticket(context)
            response = f"Unable to automatically fix error {error_id}. Ticket #{ticket_id} created."
        return response
        return "Gene FixError3Skill activated."