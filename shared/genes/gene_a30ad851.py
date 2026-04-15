"""
When a user reports a bug that has already been fixed before, this gene automatically initiates a new fix attempt to resolve the issue again.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class FixRepeatedBug(GeneBase):
    gene_id = "gene_a30ad851"
    name = "Fix Repeated Bug"
    description = """When a user reports a bug that has already been fixed before, this gene automatically initiates a new fix attempt to resolve the issue again."""
    trigger = "{'type': 'BugReport', 'condition': {'previousFixCount': {'$gt': 0}}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        if context.get("type") != "BugReport":
            return "Skipped: not a bug report."
        
        previous_fix_count = context.get("previousFixCount", 0)
        if previous_fix_count <= 0:
            return "Skipped: bug has not been fixed previously."
        
        bug_id = context.get("bugId")
        if not bug_id:
            return "Error: missing bug ID in context."
        
        previous_patch_id = context.get("previousPatchId")
        if not previous_patch_id:
            return "Error: missing previous patch ID in context."
        
        # Step 1: create a new branch for the fix
        branch_name = f"fix/{bug_id}-v2"
        await self.create_branch(branch_name)
        
        # Step 2: apply the previously used patch
        await self.apply_patch(previous_patch_id)
        
        # Step 3: run regression tests
        test_result = await self.run_tests(test_suite="regression")
        
        # Step 4: notify relevant parties
        await self.notify(
            recipients=["developer", "user"],
            message=f"Bug {bug_id} reported again. Automated fix v2 has been applied."
        )
        
        return f"Automated fix v2 applied for bug {bug_id}. Regression test result: {test_result}"
        return "Gene Fix Repeated Bug activated."
