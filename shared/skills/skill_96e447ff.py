"""
Tool 'file_read' was used 5 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentFileReadUsage(Tool):
    name = "skill_96e447ff"
    description = """Tool 'file_read' was used 5 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        recent_uses = kwargs.get("recent_file_read_uses", [])
        if len(recent_uses) >= 5:
            file_paths = [use.get("file_path") for use in recent_uses if "file_path" in use]
            return "You've called file_read {} times recently for files: {}. Consider batching reads or caching the content to improve performance.".format(len(recent_uses), ", ".join(file_paths))
        return "File read usage is within normal range."
