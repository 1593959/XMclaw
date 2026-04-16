"""
A gene that detects when a user reports a broken functionality and explicitly mentions 'error 0'. It attempts to diagnose the issue, apply a known fix if available, and escalates if the error cannot be resolved automatically.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorzero(GeneBase):
    gene_id = "gene_d5a365b2"
    name = "FixErrorZero"
    description = """A gene that detects when a user reports a broken functionality and explicitly mentions 'error 0'. It attempts to diagnose the issue, apply a known fix if available, and escalates if the error cannot be resolved automatically."""
    trigger = "UserInputContains('broken') AND UserInputContains('error 0')"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Parse user message
        user_message = context.get('user_message', '')
        if 'error 0' in user_message.lower() and 'broken' in user_message.lower():
            # Log the issue
            logger.error('User reported broken functionality: error 0')
            # Perform diagnostic steps
            diagnostic_result = self._diagnose_error_0()
            if diagnostic_result['found']:
                # Apply fix
                fix_result = self._apply_fix(diagnostic_result['solution'])
                return {'status': 'fixed', 'message': fix_result['message']}
            else:
                # No known solution, escalate
                return {'status': 'escalated', 'message': 'Error 0 could not be automatically resolved.'}
        else:
            # Not our trigger, pass through
            return {'status': 'noop'}
        return "Gene FixErrorZero activated."
