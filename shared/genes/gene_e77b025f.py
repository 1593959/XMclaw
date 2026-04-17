"""
Detects when a user reports the same bug again after a previous fix attempt and automatically initiates a fix process.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class BugFixRepeatTrigger(GeneBase):
    gene_id = "gene_e77b025f"
    name = "Bug Fix Repeat Trigger"
    description = """Detects when a user reports the same bug again after a previous fix attempt and automatically initiates a fix process."""
    trigger = "{'type': 'bug_report_repeat', 'conditions': {'bug_id': '<any>', 'report_reoccurrence': True, 'previous_fix_status': 'attempted'}}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        bug_id = context.get("bug_id")
        user_input = context.get("user_input", "")
        report_reoccurrence = context.get("report_reoccurrence")
        previous_fix_status = context.get("previous_fix_status")
        
        # Verify trigger conditions are met
        if not report_reoccurrence or previous_fix_status != "attempted":
            return f"Trigger conditions not met. Bug report must be a recurrence with previous fix attempt."
        
        # Execute automated fix steps in sequence
        try:
            # Step 1: Analyze stack trace
            analysis_result = await self.analyze_stack_trace(user_input)
            if not analysis_result.get("success"):
                await self.escalate_to_developer(bug_id, "analyze_stack_trace", analysis_result.get("error"))
                return f"Analysis failed for bug {bug_id}. Escalated to developer."
        
            # Step 2: Apply prepared patch
            patch_result = await self.apply_prepared_patch(bug_id)
            if not patch_result.get("success"):
                await self.escalate_to_developer(bug_id, "apply_prepared_patch", patch_result.get("error"))
                return f"Failed to apply patch for bug {bug_id}. Escalated to developer."
        
            # Step 3: Validate fix
            validation_result = await self.validate_fix(bug_id)
            if not validation_result.get("success"):
                await self.escalate_to_developer(bug_id, "validate_fix", validation_result.get("error"))
                return f"Fix validation failed for bug {bug_id}. Escalated to developer."
        
            # Step 4: Notify user
            await self.notify_user(bug_id)
            return f"Successfully fixed and verified bug {bug_id}."
        
        except Exception as e:
            await self.escalate_to_developer(bug_id, "fix_process", str(e))
            return f"Unexpected error during fix process for bug {bug_id}. Escalated to developer."
        return "Gene Bug Fix Repeat Trigger activated."