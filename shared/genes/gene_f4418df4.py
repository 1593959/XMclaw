"""
Skill that automatically addresses user reports of a broken feature accompanied by error 4. It detects the issue in the user message, diagnoses the likely cause, and returns step‑by‑step fix instructions.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_f4418df4"
    name = "FixError4Skill"
    description = """Skill that automatically addresses user reports of a broken feature accompanied by error 4. It detects the issue in the user message, diagnoses the likely cause, and returns step‑by‑step fix instructions."""
    trigger = "User message matches pattern: contains the word 'broken' (case‑insensitive) AND the phrase 'error 4' (case‑insensitive)."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_msg = context.get('user_message', '')
        if 'error 4' in error_msg.lower():
            # Identify root cause and provide fix steps
            fix_steps = [
                'Check the logs for error 4 details.',
                'Verify the configuration file for missing entries.',
                'Restart the affected service.',
                'Run the diagnostic script to reinitialize the module.',
                'If issue persists, escalate to support.'
            ]
            return {'message': 'Error 4 detected. Follow these steps to resolve:', 'fix_steps': fix_steps}
        else:
            return {'message': 'No error 4 detected in your report.'}
        return "Gene FixError4Skill activated."
