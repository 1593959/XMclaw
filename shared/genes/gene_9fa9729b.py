"""
When user reports a bug, proactively suggest running tests.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Autobugfixgene(GeneBase):
    gene_id = "gene_9fa9729b"
    name = "AutoBugFixGene"
    description = """When user reports a bug, proactively suggest running tests."""
    trigger = "bug"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "").lower()
        if "bug" in user_input or "error" in user_input or "issue" in user_input:
            return "It looks like you've reported a bug. I recommend running the test suite (e.g., pytest) to verify the issue."
        return ""
        return "Gene AutoBugFixGene activated."
