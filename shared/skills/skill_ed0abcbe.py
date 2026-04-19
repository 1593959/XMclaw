"""
Tool 'file_read' was used 5 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool


class AutoFrequentFileReadUsage(Tool):
    name = "skill_ed0abcbe"
    description = "Tool 'file_read' was used 5 times recently."

    parameters = {
        "input": {
            "type": "string",
            "description": "Input for the skill"
        }
    }

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        count = kwargs.get("count", 5)
        files = kwargs.get("files", [])
        if files:
            file_summary = ", ".join(files)
            suggestion = (
                f"You have used the file_read tool {count} times recently, "
                f"including: {file_summary}. "
                "Consider batching file reads or caching the contents for better performance."
            )
        else:
            suggestion = (
                f"You have used the file_read tool {count} times recently. "
                "Consider batching file reads or caching the contents for better performance."
            )
        return suggestion
