"""Auto-generated Gene for XMclaw.
This gene detects when a bug that was previously fixed is being reported or fixed again. It triggers a deeper investigation into why the fix didn't hold, identifying potential root causes such as incomplete fixes, regressions, or architectural issues that keep spawning the same bug.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase


class BugFixRegressionDetector(GeneBase):
    gene_id = "gene_65bc3fcb"
    name = "Bug Fix Regression Detector"
    description = "This gene detects when a bug that was previously fixed is being reported or fixed again. It triggers a deeper investigation into why the fix didn't hold, identifying potential root causes such as inco"
    trigger = "{'event': 'bug_fix_reopened', 'conditions': ["

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return bool(self.trigger and self.trigger.lower() in user_input.lower())

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        return "Gene Bug Fix Regression Detector activated."
