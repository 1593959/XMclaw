"""
This gene ensures that any fix applied to a user‑reported bug actually resolves the issue and does not introduce regressions. If the fix fails validation, it is automatically reverted and the responsible developer is notified.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class ValidateBugFixCorrectness(GeneBase):
    gene_id = "gene_bba52615"
    name = "Validate Bug Fix Correctness"
    description = """This gene ensures that any fix applied to a user‑reported bug actually resolves the issue and does not introduce regressions. If the fix fails validation, it is automatically reverted and the responsible developer is notified."""
    trigger = "Event: Bug_Fix_Commit – a commit is pushed that claims to address a user‑reported issue; Condition: a corresponding bug report exists in the tracking system."

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        commit_id = context.get("commit_id")
            bug_id = context.get("bug_id")
            repo_url = context.get("repo_url")
        
            if not commit_id or not bug_id:
                return "Missing commit_id or bug_id in context."
        
            # Run the project's regression test suite
            regression_results = await self.test_runner.run_regression_suite(repo_url, commit_id)
        
            # Run bug‑specific validation tests
            bug_validation_results = await self.test_runner.run_bug_validation_tests(repo_url, commit_id, bug_id)
        
            # Verify that the reported bug can no longer be reproduced
            bug_still_present = await self.bug_tracker.verify_bug_reproduction(repo_url, bug_id)
        
            # Determine overall success
            tests_passed = regression_results.get("passed", False) and bug_validation_results.get("passed", False)
        
            if not tests_passed or bug_still_present:
                # Automatically revert the commit
                revert_success = await self.vcs.revert_commit(repo_url, commit_id)
        
                # Prepare a summary of the failure
                summary = {
                    "commit_id": commit_id,
                    "bug_id": bug_id,
                    "regression_tests_passed": regression_results.get("passed", False),
                    "bug_validation_passed": bug_validation_results.get("passed", False),
                    "bug_still_present": bug_still_present,
                    "revert_success": revert_success,
                }
        
                # Alert the development team
                await self.notification_service.send_alert(
                    subject=f"Bug fix validation failed for commit {commit_id}",
                    body=summary
                )
        
                return f"Validation failed for commit {commit_id}. Reverted changes. Team alerted."
        
            return f"Bug fix for commit {commit_id} validated successfully."
        return "Gene Validate Bug Fix Correctness activated."
