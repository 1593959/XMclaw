"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_71e71544"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Retrieve recent bash commands from the context
        recent = self.context.get('recent_bash_commands', [])
        if not recent:
            self.output('No recent bash commands to batch.')
            return
        # Merge commands into a single script
        script = '\n'.join(recent)
        self.output('Proposed batch script:\n' + script)
        # Prompt user for confirmation
        if self.prompt_confirm('Run the combined script?'):
            result = self.run_bash(script)
            self.output('Batch execution result:\n' + str(result))
