"""
Skill that automatically handles user reports of 'this is broken, please fix error 0' by diagnosing the issue and attempting to resolve error code 0.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Fixerror0skill(GeneBase):
    gene_id = "gene_400fa50a"
    name = "FixError0Skill"
    description = """Skill that automatically handles user reports of 'this is broken, please fix error 0' by diagnosing the issue and attempting to resolve error code 0."""
    trigger = "{'type': 'phrase', 'patterns': ['this is broken, please fix error 0', 'error 0 is broken']}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        error_code = "0"
        logger.info(f"User reported broken state for error {error_code}")
        context = self.get_context(error_code)
        if context:
            self.fix_issue(context)
            logger.info(f"Successfully fixed error {error_code}")
        else:
            logger.warning(f"No known fix for error {error_code}")
            self.notify_support(error_code)
        return "Gene FixError0Skill activated."