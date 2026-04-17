"""
Detects user reports of broken functionality or specific errors (e.g., error 3) and logs them for resolution.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerrorreporter(GeneBase):
    gene_id = "gene_2b120ee9"
    name = "FixErrorReporter"
    description = """Detects user reports of broken functionality or specific errors (e.g., error 3) and logs them for resolution."""
    trigger = "User message contains 'broken' or 'error'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_message = context.get('user_message', '')
        if 'error 3' in user_message.lower():
            log_entry = {
                'issue': 'error 3',
                'message': user_message,
                'timestamp': context.get('timestamp', 'unknown')
            }
            context.setdefault('error_log', []).append(log_entry)
            return 'We have identified error 3 and are working to fix it.'
        elif 'broken' in user_message.lower() or 'error' in user_message.lower():
            log_entry = {
                'issue': 'general error',
                'message': user_message,
                'timestamp': context.get('timestamp', 'unknown')
            }
            context.setdefault('error_log', []).append(log_entry)
            return 'We have logged your report and will address the issue shortly.'
        return "Gene FixErrorReporter activated."