"""
Skill to automatically detect and resolve error 2 reported by users.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2skill(GeneBase):
    gene_id = "gene_6d879985"
    name = "FixError2Skill"
    description = """Skill to automatically detect and resolve error 2 reported by users."""
    trigger = "User reports 'broken' and mentions 'error 2', or error code 2 is present in the system context."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_info = context.get('error_details')
        if error_info and error_info.get('code') == 2:
            logger.error(f"Error 2 detected: {error_info}")
            fix_result = fix_broken_component()
            if fix_result:
                return {'status': 'fixed', 'message': 'Error 2 has been resolved.', 'details': fix_result}
            else:
                return {'status': 'failed', 'message': 'Failed to fix error 2.'}
        else:
            return {'status': 'ignored', 'message': 'No error 2 detected.'}
        return "Gene FixError2Skill activated."
