"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_4a085dc0"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Retrieve the most recent bash command
        last_cmd = context.get_last_tool_input('bash')
        # Provide a concise explanation
        explanation = context.run_tool('explain', command=last_cmd)
        # Optionally generate a reusable script snippet
        script = context.run_tool('generate_script', command=last_cmd)
        # Return the results
        return {'explanation': explanation, 'script': script}
