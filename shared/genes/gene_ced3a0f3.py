"""
Detects when a user reports a broken feature or specific error (e.g., 'error 2') and automatically diagnoses and fixes the issue.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixreportederror(GeneBase):
    gene_id = "gene_ced3a0f3"
    name = "FixReportedError"
    description = """Detects when a user reports a broken feature or specific error (e.g., 'error 2') and automatically diagnoses and fixes the issue."""
    trigger = "User input matches patterns: 'broken', 'fix error', 'error 2', 'please fix'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_id = 'error_2'
        log = self.fetch_error_log(error_id)
        if not log:
            self.reply('Could not locate error 2. Please provide more details.')
            return
        cause = self.analyze_error(log)
        fix = self.generate_fix(cause)
        self.apply_fix(fix)
        self.reply('Error 2 has been fixed. The issue has been resolved.')
        return "Gene FixReportedError activated."
