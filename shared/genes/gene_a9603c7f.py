"""
When a bug is reported as unfixed or regressed, this gene ensures thorough re-testing and validation before closing the issue, preventing recurring bug reports.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugFixVerification(GeneBase):
    gene_id = "gene_a9603c7f"
    name = "Bug Fix Verification"
    description = """When a bug is reported as unfixed or regressed, this gene ensures thorough re-testing and validation before closing the issue, preventing recurring bug reports."""
    trigger = "{'type': 'event', 'condition': "issue.status == 'reopened' AND issue.labels CONTAINS 'bug' AND issue.comment CONTAINS 'fix the bug once more'"}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        issue = context.get("issue", {})
            status = issue.get("status", "")
            labels = issue.get("labels", [])
            comment = issue.get("comment", "")
        
            if status == "reopened" and "bug" in labels and "fix the bug once more" in comment.lower():
                # 1. Revert the previous fix if available
                revert_msg = None
                fix_commit = issue.get("fix_commit")
                if fix_commit:
                    revert_msg = await self.revert_commit(fix_commit)
        
                # 2. Re-run automated test suite against the bug scenario
                test_result = await self.run_test_suite(issue.get("test_scenario_id"))
        
                # 3. Generate detailed reproduction steps
                reproduction_steps = self.generate_reproduction_steps(issue)
        
                # 4. Assign to original developer or senior engineer
                assignee = issue.get("original_assignee") or issue.get("senior_engineer")
                await self.update_assignee(issue.get("id"), assignee)
        
                # 5. Require code review with focus on edge cases
                await self.add_comment(
                    issue.get("id"),
                    "Code review required: focus on edge cases and possible regressions."
                )
        
                # 6. Add 'requires-validation' label
                if "requires-validation" not in labels:
                    labels.append("requires-validation")
                    await self.update_labels(issue.get("id"), labels)
        
                # 7. Notify QA for manual verification before closing
                await self.send_notification(
                    message="Bug requires re-fix. Previous fix was insufficient. Please investigate thoroughly.",
                    recipients=["developer", "qa_team"]
                )
        
                result = (
                    f"Bug fix verification triggered for issue {issue.get('id')}. "
                    f"Reverted previous commit: {revert_msg}. "
                    f"Test suite result: {test_result}. "
                    f"Reproduction steps: {reproduction_steps}. "
                    f"Assigned to {assignee}. "
                    f"Added 'requires-validation' label. "
                    f"QA notified."
                )
            else:
                result = "No bug fix verification needed; trigger conditions not met."
        
            return result
        return "Gene Bug Fix Verification activated."
