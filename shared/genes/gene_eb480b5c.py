"""
Automatically re-fixes a bug that is reported again after it has already been fixed once. This gene triggers when a previously resolved bug is reopened, runs the same fix steps that were used before, notifies the team, and updates the issue status.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class FixBugReOccurrence(GeneBase):
    gene_id = "gene_eb480b5c"
    name = "Fix Bug Re-occurrence"
    description = """Automatically re-fixes a bug that is reported again after it has already been fixed once. This gene triggers when a previously resolved bug is reopened, runs the same fix steps that were used before, notifies the team, and updates the issue status."""
    trigger = "{'type': 'issue_reopened', 'conditions': {'label': 'bug', 'previous_fix_applied': True}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        issue = context.get("issue", {})
        if not issue:
            return "Error: No issue data found in context."
        
        # Verify trigger conditions
        event_type = issue.get("type") or context.get("event_type")
        if event_type != "issue_reopened":
            return f"Trigger condition not met: event type '{event_type}' is not 'issue_reopened'."
        
        labels = issue.get("labels", [])
        if "bug" not in labels:
            return "Trigger condition not met: issue does not have 'bug' label."
        
        if not issue.get("previous_fix_applied"):
            return "Trigger condition not met: previous fix not recorded."
        
        # Retrieve previous fix details
        fix_data = issue.get("previous_fix", {})
        diagnostic_scripts = fix_data.get("diagnostic_scripts", [])
        patch = fix_data.get("patch")
        regression_tests = fix_data.get("regression_tests")
        
        # Step 1: Re-run diagnostic scripts that identified the original bug
        diagnostic_results = []
        if diagnostic_scripts:
            try:
                diagnostic_results = await run_diagnostic_scripts(scripts=diagnostic_scripts, issue_id=issue["id"])
            except Exception as e:
                return f"Failed to run diagnostic scripts: {e}"
        else:
            diagnostic_results.append("No diagnostic scripts recorded.")
        
        # Step 2: Apply the same patch that resolved the bug previously
        apply_result = ""
        if patch:
            try:
                apply_result = await apply_patch(patch=patch, issue_id=issue["id"])
            except Exception as e:
                return f"Failed to apply patch: {e}"
        else:
            apply_result = "No patch to apply."
        
        # Step 3: Run regression tests to confirm the fix works
        regression_result = ""
        if regression_tests:
            try:
                regression_result = await run_regression_tests(tests=regression_tests, issue_id=issue["id"])
            except Exception as e:
                return f"Failed to run regression tests: {e}"
        else:
            regression_result = "No regression tests recorded."
        
        # Step 4: Notify the development team of the re-fix
        notification_message = (
            f"Bug '{issue.get('title')}' (ID: {issue['id']}) has been automatically re-fixed "
            "after being reopened. Previous fix steps were re-applied."
        )
        try:
            notification = await notify_team(channel="dev-team", message=notification_message)
        except Exception as e:
            return f"Failed to notify team: {e}"
        
        # Step 5: Update the issue status to "resolved" and add a comment noting the repeat fix
        comment = (
            "This issue was automatically re-fixed using the same fix steps that resolved it previously. "
            f"Diagnostic scripts: {diagnostic_results}. "
            f"Patch application: {apply_result}. "
            f"Regression tests: {regression_result}."
        )
        try:
            update_result = await update_issue_status(
                issue_id=issue["id"],
                status="resolved",
                comment=comment
            )
        except Exception as e:
            return f"Failed to update issue status: {e}"
        
        # Compose final result summary
        return (
            f"Bug re-fix completed successfully. "
            f"Diagnostic results: {diagnostic_results}. "
            f"Patch applied: {apply_result}. "
            f"Regression tests: {regression_result}. "
            f"Team notified: {notification}. "
            f"Issue updated: {update_result}."
        )
        return "Gene Fix Bug Re-occurrence activated."