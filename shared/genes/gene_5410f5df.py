"""
Skill that automatically diagnoses and remedies error 2 when a user reports that something is broken.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_5410f5df"
    name = "FixError2Skill"
    description = """Skill that automatically diagnoses and remedies error 2 when a user reports that something is broken."""
    trigger = "User says 'this is broken, please fix error 2'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve recent logs for the affected service
        logs = get_service_logs('service_name')
        # Parse logs to find error code 2
        error_entry = next((line for line in logs if 'error_2' in line), None)
        if error_entry:
            # Extract error details and context
            error_info = parse_error_entry(error_entry)
            # Apply the predefined fix for error_2
            apply_fix('error_2', error_info)
            return {"status": "fixed", "error_info": error_info}
        else:
            return {"status": "error_not_found"}
        return "Gene FixError2Skill activated."
