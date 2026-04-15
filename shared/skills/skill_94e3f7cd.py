"""
Tool 'bash' was used 44 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_94e3f7cd"
    description = """Tool 'bash' was used 44 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        usage_count = kwargs.get("usage_count", 44)
        recent_commands = kwargs.get("recent_commands", [])
        
        # Provide a suggestion based on usage frequency
        if usage_count > 30:
            suggestion = (
                "You have used the 'bash' tool 44 times recently. "
                "Consider automating repetitive tasks by creating a shell script or defining an alias."
            )
        else:
            suggestion = (
                f"You have used the 'bash' tool {usage_count} times recently. "
                "Your usage is moderate."
            )
        
        # Format recent commands if any
        if recent_commands:
            cmd_list = "\n".join(f"  - {cmd}" for cmd in recent_commands[-5:])
            recent_summary = f"Recent commands:\n{cmd_list}"
        else:
            recent_summary = "No recent commands provided."
        
        return f"{suggestion}\n{recent_summary}"
