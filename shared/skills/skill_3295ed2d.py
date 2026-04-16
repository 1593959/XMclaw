"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_3295ed2d"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Retrieve recent bash commands from the conversation context
        recent_commands = context.get_recent_tool_calls('bash')
        # Count occurrences of each command
        command_counts = {}
        for cmd in recent_commands:
            command_counts[cmd] = command_counts.get(cmd, 0) + 1
        # Identify the most frequent command
        if command_counts:
            most_common_cmd, count = max(command_counts.items(), key=lambda x: x[1])
            if count >= 3:
                suggestion = (
                    f"You've executed the command \"{most_common_cmd}\" "
                    f"{count} times recently. "
                    f"Would you like to create a script or alias for it?"
                )
                return {"type": "suggestion", "content": suggestion}
        return None
