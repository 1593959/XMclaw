"""
Skill that automatically detects user reports of error 3, diagnoses the underlying cause, and applies the appropriate fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Error3fixer(GeneBase):
    gene_id = "gene_80dec9e3"
    name = "Error3Fixer"
    description = """Skill that automatically detects user reports of error 3, diagnoses the underlying cause, and applies the appropriate fix."""
    trigger = "User message contains phrases such as "error 3", "error three", "broken", or "please fix error 3"."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        # Parse the incoming user report
        report = context.get('message', '')
        if 'error 3' not in report.lower() and 'error three' not in report.lower():
            return {'status': 'ignored'}
        
        # Retrieve relevant logs and environment details
        user_id = context.get('user_id')
        logs = fetch_recent_logs(user_id)
        config = load_configuration()
        
        # Identify root cause of error 3
        root_cause = analyze_error(logs, config)
        
        # Apply the appropriate fix based on root cause
        if root_cause == 'missing_setting':
            set_required_setting(config)
            save_configuration(config)
        elif root_cause == 'corrupted_data':
            repair_corrupted_data()
        else:
            log_warning('Unknown root cause for error 3; applying generic fix.')
            generic_fix()
        
        # Verify the fix was successful
        if verify_fix(user_id):
            send_reply(user_id, 'Error 3 has been resolved. Please try again.')
        else:
            send_reply(user_id, 'Failed to automatically fix error 3. Support has been notified.')
            notify_support(user_id)
        return "Gene Error3Fixer activated."
