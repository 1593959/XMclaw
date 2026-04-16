"""
Automatically responds to user reports of broken functionality labeled as error 4 by diagnosing the issue and performing corrective actions such as reloading configuration and restarting the affected service.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Skillfixerror4(GeneBase):
    gene_id = "gene_7b37810e"
    name = "SkillFixError4"
    description = """Automatically responds to user reports of broken functionality labeled as error 4 by diagnosing the issue and performing corrective actions such as reloading configuration and restarting the affected service."""
    trigger = "User message containing 'fix error 4' or 'this is broken'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_info = context.get('error_4')
        if error_info:
            print('Error 4 detected')
            service = context.get('service')
            if service:
                service.reload_config()
                service.restart()
                print('Fix applied successfully')
            else:
                print('Service not found')
        else:
            print('No error 4 info')
        return "Gene SkillFixError4 activated."
