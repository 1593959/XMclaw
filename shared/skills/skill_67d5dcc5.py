"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_67d5dcc5"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        tool_name = kwargs.get("tool_name", "bash")
        usage_count = kwargs.get("usage_count", 10)
        
        suggestion = "Consider batching your commands or writing a script to reduce repeated bash calls."
        return f"The '{tool_name}' tool has been used {usage_count} times recently. {suggestion}"
