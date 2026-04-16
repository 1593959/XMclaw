"""
A skill that automatically detects and resolves the specific error reported by the user (error 2). It extracts the error number, retrieves error logs, runs a root‑cause analysis, attempts to apply a known fix, and reports back to the user.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Errorfixer(GeneBase):
    gene_id = "gene_8806ab1c"
    name = "ErrorFixer"
    description = """A skill that automatically detects and resolves the specific error reported by the user (error 2). It extracts the error number, retrieves error logs, runs a root‑cause analysis, attempts to apply a known fix, and reports back to the user."""
    trigger = "User says something like 'this is broken, please fix error 2' or any phrase that indicates a specific error needs fixing."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_num = extract_error_number(context.user_message)
        
        # Retrieve the error log for the reported error
        error_log = logging_service.get_error_log(error_num)
        
        # Perform root‑cause analysis
        analysis = analyzer.analyze(error_log)
        
        # If a fix is known, apply it; otherwise, alert the user
        if analysis.has_fix:
            fix_result = fixer.apply(analysis.fix_id)
            response = f'Error {error_num} has been automatically fixed: {fix_result.summary}'
        else:
            response = f'Unable to automatically resolve error {error_num}. Please review the detailed logs.'
        
        # Notify the user of the result
        notify_user(response)
        return "Gene ErrorFixer activated."
