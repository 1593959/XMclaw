"""
Skill to handle user reports of 'error 0' by logging, diagnosing, resetting the faulty component, and retrying the operation.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0(GeneBase):
    gene_id = "gene_f37d5d15"
    name = "FixError0"
    description = """Skill to handle user reports of 'error 0' by logging, diagnosing, resetting the faulty component, and retrying the operation."""
    trigger = "User says 'this is broken, please fix error 0' or matches pattern 'error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logger.error("Error 0 reported by user")
        error_info = self.get_error_details(0)
        if not error_info:
            self.user.notify("No details found for error 0.")
            return
        component = error_info.get("component")
        self.reset_component(component)
        result = self.retry_operation(error_info.get("operation_id"))
        if result.success:
            self.user.notify("Error 0 has been fixed and operation completed successfully.")
        else:
            self.user.notify("Failed to fix error 0. Please contact support.")
        return "Gene FixError0 activated."
