"""
When a user reports the same bug again after it has already been fixed (e.g., the user says 'fix the bug once more'), this Gene automatically retrieves the most recent fix, re-applies it, runs regression tests, and informs the user of the result.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class Re_fixBugAssistant(GeneBase):
    gene_id = "gene_02b40b3b"
    name = "Re-fix Bug Assistant"
    description = """When a user reports the same bug again after it has already been fixed (e.g., the user says 'fix the bug once more'), this Gene automatically retrieves the most recent fix, re-applies it, runs regression tests, and informs the user of the result."""
    trigger = "User submits a bug report that contains the phrase 'fix the bug once more'"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        if "fix the bug once more" not in user_input.lower():
            return "Trigger phrase not detected. No re-fix action taken."
        
        # Try to extract a bug identifier from the user input (e.g., "Bug #123" or "BUG-123")
        bug_id = None
        match = re.search(r'(?:bug\s?#?\s*(\d+)|BUG[-\s]?(\d+))', user_input, re.IGNORECASE)
        if match:
            bug_id = match.group(1) or match.group(2)
        
        # Fallback: use bug_id passed in the context
        if not bug_id:
            bug_id = context.get("bug_id")
        
        if not bug_id:
            return "Could not determine the bug identifier. Please specify a bug ID."
        
        try:
            # 1. Retrieve the latest known fix for the identified bug
            fix = await self.fix_store.get_latest_fix(bug_id)
            if not fix:
                await self.team_notifier.escalate_to_team(
                    bug_id,
                    f"No recorded fix found for bug {bug_id}."
                )
                return f"No fix found for bug {bug_id}. Issue has been escalated to the development team."
        
            # 2. Re-apply the fix to the codebase
            applied = await self.patch_applier.apply_fix(fix)
            if not applied:
                await self.team_notifier.escalate_to_team(
                    bug_id,
                    f"Failed to apply the patch for bug {bug_id}."
                )
                return f"Failed to apply fix for bug {bug_id}. Issue escalated."
        
            # 3. Execute the associated regression test suite
            tests_passed = await self.test_runner.run_regression_tests()
        
            if tests_passed:
                # 4. Tests passed - mark the bug as resolved and notify the user
                await self.bug_tracker.resolve_bug(bug_id)
                user_id = context.get("user_id", "user")
                await self.user_notifier.notify_user(
                    user_id,
                    f"Bug {bug_id} has been re-fixed and all regression tests passed. The bug is now marked as resolved."
                )
                return f"Bug {bug_id} re-fixed successfully. Regression tests passed. Bug marked as resolved."
            else:
                # 5. Tests failed - escalate to the development team
                await self.team_notifier.escalate_to_team(
                    bug_id,
                    f"Regression tests failed after re-applying fix for bug {bug_id}."
                )
                user_id = context.get("user_id", "user")
                await self.user_notifier.notify_user(
                    user_id,
                    f"Bug {bug_id} was re-fixed but regression tests failed. The issue has been escalated to the development team."
                )
                return f"Bug {bug_id} re-fixed but regression tests failed. Issue escalated to the development team."
        
        except Exception as e:
            # Unexpected error - escalate and report
            await self.team_notifier.escalate_to_team(
                bug_id,
                f"Unexpected error while re-fixing bug {bug_id}: {e}"
            )
            return f"An unexpected error occurred while re-fixing bug {bug_id}. The issue has been escalated."
        return "Gene Re-fix Bug Assistant activated."