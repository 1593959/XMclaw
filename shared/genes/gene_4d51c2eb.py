"""
A skill that automatically identifies and resolves error 0 when a user reports it.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0(GeneBase):
    gene_id = "gene_4d51c2eb"
    name = "FixError0"
    description = """A skill that automatically identifies and resolves error 0 when a user reports it."""
    trigger = "User says 'this is broken, please fix error 0'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        try:
            log('User reported error 0: "this is broken, please fix error 0"')
            diagnostic = run_diagnostic('error_0')
            if diagnostic.get('found'):
                fix_error_0()
                respond('Error 0 has been fixed.')
            else:
                respond('No error 0 detected.')
        except Exception as e:
            log(f'Error while fixing error 0: {e}')
            respond('Failed to fix error 0.')
        return "Gene FixError0 activated."