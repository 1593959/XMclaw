"""
A skill that automatically diagnoses and fixes error 4 reported by the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4skill(GeneBase):
    gene_id = "gene_ee6372a1"
    name = "FixError4Skill"
    description = """A skill that automatically diagnoses and fixes error 4 reported by the user."""
    trigger = "User says 'this is broken, please fix error 4' or any similar phrase referencing error 4."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = 4
        log_entry = find_log_entry(error_code)
        if log_entry:
            cause = analyze_error(log_entry)
            fix = generate_fix(cause)
            apply_fix(fix)
            notify_user('Error 4 has been fixed successfully.')
        else:
            notify_user('Could not locate error 4 in logs. Please provide more details.')
        return "Gene FixError4Skill activated."
