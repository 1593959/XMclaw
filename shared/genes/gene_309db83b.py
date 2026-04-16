"""
A skill that automatically detects user reports of a broken state or error 0 and attempts to resolve the issue.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorzeroskill(GeneBase):
    gene_id = "gene_309db83b"
    name = "FixErrorZeroSkill"
    description = """A skill that automatically detects user reports of a broken state or error 0 and attempts to resolve the issue."""
    trigger = "error.?0|broken|fix"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        log('User reported issue: ' + context.user_message)
        if 'error 0' in context.user_message.lower() or 'broken' in context.user_message.lower():
            # Run diagnostics to identify the root cause
            diagnostic_result = diagnostic.run(context)
            if diagnostic_result.has_issue:
                # Apply the fix for error 0
                fix_result = fix.apply(context)
                return {'status': 'fixed', 'message': fix_result}
            else:
                return {'status': 'already_fixed', 'message': 'No issue detected'}
        else:
            return {'status': 'no_action', 'message': 'Trigger not matched'}
        return "Gene FixErrorZeroSkill activated."
