"""
Skill to automatically detect and resolve error code 2 reported by users
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror2(GeneBase):
    gene_id = "gene_8f536c5d"
    name = "FixError2"
    description = """Skill to automatically detect and resolve error code 2 reported by users"""
    trigger = "User reports error 2 or system logs contain error code 2"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        logs = fetch_recent_logs()
        for log in logs:
            if 'error 2' in log.lower():
                print('Detected error 2, initiating fix...')
                run_fix_script('fix_error_2')
                break
        return "Gene FixError2 activated."