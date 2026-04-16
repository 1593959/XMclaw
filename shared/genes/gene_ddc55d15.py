"""
Skill that detects a user request to fix error 4, retrieves recent logs, identifies occurrences of 'error 4', parses the context, and applies a known fix if available.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror4(GeneBase):
    gene_id = "gene_ddc55d15"
    name = "FixError4"
    description = """Skill that detects a user request to fix error 4, retrieves recent logs, identifies occurrences of 'error 4', parses the context, and applies a known fix if available."""
    trigger = "User input containing 'fix error 4' or 'this is broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Retrieve recent logs
        logs = get_recent_logs(limit=100)
        # Filter lines containing 'error 4'
        error_lines = [line for line in logs if 'error 4' in line.lower()]
        if not error_lines:
            return 'No error 4 found in recent logs.'
        for line in error_lines:
            # Parse error context
            context = parse_error_entry(line)
            # Obtain known fix for the context
            fix = get_fix_for_error(context)
            if fix:
                apply_fix(fix)
                return 'Fixed error 4 successfully.'
            else:
                return 'No known fix available for this error 4 occurrence.'
        return 'Error 4 could not be resolved.'
        return "Gene FixError4 activated."
