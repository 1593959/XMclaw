"""
Automatically detects and remediates error 4 when a user reports a broken system.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error4fixskill(GeneBase):
    gene_id = "gene_ec233594"
    name = "Error4FixSkill"
    description = """Automatically detects and remediates error 4 when a user reports a broken system."""
    trigger = "User says 'this is broken, please fix error 4' or logs contain 'error 4'."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            error_logs = self.read_logs()
            error4_entries = [line for line in error_logs if "error 4" in line.lower()]
            if error4_entries:
                self.restart_service()
                self.clear_cache()
                self.notify_user("Error 4 has been fixed.")
            else:
                self.notify_user("No error 4 found.")
        except Exception as e:
            self.notify_user(f"Failed to fix error 4: {e}")
        return "Gene Error4FixSkill activated."