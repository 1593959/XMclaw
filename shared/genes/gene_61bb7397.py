"""
Automatically detects and resolves error 3 reported by users, diagnosing the broken component and applying a fix.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror3skill(GeneBase):
    gene_id = "gene_61bb7397"
    name = "FixError3Skill"
    description = """Automatically detects and resolves error 3 reported by users, diagnosing the broken component and applying a fix."""
    trigger = "User input contains 'broken' and 'error 3' (case‑insensitive), e.g., 'this is broken, please fix error 3'."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
        	# Identify the service or component that reported error 3
        	service_name = detect_error_source(error_code='3')
        	# Retrieve recent logs for the service
        	logs = get_recent_logs(service_name, lines=50)
        	# Parse logs to locate the root cause of error 3
        	cause = parse_error(logs, error_code='3')
        	# Attempt to apply known fix or restart the component
        	apply_fix(service_name, cause)
        	# Verify fix by re-running health check
        	health = check_health(service_name)
        	if health.status == 'healthy':
        		return {'status': 'fixed', 'message': 'Error 3 resolved successfully.'}
        	else:
        		return {'status': 'failed', 'message': 'Fix attempted but service still unhealthy.'}
        except Exception as e:
        		return {'status': 'error', 'message': str(e)}
        return "Gene FixError3Skill activated."
