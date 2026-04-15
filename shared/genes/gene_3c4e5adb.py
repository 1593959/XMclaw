"""
Triggers a new bug‑fix workflow when a user explicitly requests to fix the bug again.
Auto-generated Gene for XMclaw.
"""
from xmclaw.genes.base import GeneBase

class FixBugAgain(GeneBase):
    gene_id = "gene_3c4e5adb"
    name = "Fix Bug Again"
    description = """Triggers a new bug‑fix workflow when a user explicitly requests to fix the bug again."""
    trigger = "{'type': 'user_action', 'event': 'click', 'element': 'fix_again_button'}"

    async def evaluate(self, context: dict) -> bool:
        """Return True if this gene should activate."""
        user_input = context.get("user_input", "")
        return self.trigger.lower() in user_input.lower()

    async def execute(self, context: dict) -> str:
        """Execute the gene's action."""
        user_input = context.get("user_input", "")
        agent_id = context.get("agent_id", "")
        
        # Log the request (optional)
        self.logger.info(f"Bug fix again requested. Agent: {agent_id}, Input: {user_input}")
        
        # Trigger the bug fix workflow
        workflow_result = await self.trigger_workflow(
            workflow_id="bug_fix_pipeline",
            parameters={"notify_team": True, "assign_to": "development"}
        )
        
        # Provide feedback based on result
        if workflow_result and workflow_result.get("status") == "success":
            return f"Bug fix pipeline triggered successfully. Workflow ID: {workflow_result.get('id', 'unknown')}"
        else:
            return "Bug fix pipeline could not be started."
        return "Gene Fix Bug Again activated."
