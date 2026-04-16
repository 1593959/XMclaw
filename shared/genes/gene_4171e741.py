"""
A skill that listens for user reports of a broken feature involving error 3, locates the error in the system logs, attempts to apply a fix, and confirms resolution to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_4171e741"
    name = "FixError3Skill"
    description = """A skill that listens for user reports of a broken feature involving error 3, locates the error in the system logs, attempts to apply a fix, and confirms resolution to the user."""
    trigger = "User reports a broken issue and mentions 'error 3' in the same message"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        context = skill_context
        user_msg = context.user_message
        log = context.get_logs()
        error_line = next((line for line in log if "error 3" in line.lower()), None)
        if error_line:
            fix = parse_fix(error_line)
            context.apply_fix(fix)
            context.respond("Fixed error 3: " + fix)
        else:
            context.respond("Could not locate error 3 in logs.")
        return "Gene FixError3Skill activated."
