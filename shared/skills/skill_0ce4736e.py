"""
Tool 'file_read' was used 5 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentFileReadUsage(Tool):
    name = "skill_0ce4736e"
    description = """Tool 'file_read' was used 5 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        file_read_count = kwargs.get("file_read_count", 5)
        recent_files = kwargs.get("recent_files", [])
        tool_name = kwargs.get("tool_name", "file_read")
        
        if recent_files:
            files_summary = ", ".join(recent_files[:3])
            if len(recent_files) > 3:
                files_summary += f" and {len(recent_files) - 3} more"
            return f"Detected {file_read_count} recent uses of '{tool_name}' tool. Recently accessed files: {files_summary}"
        else:
            return f"Detected {file_read_count} recent uses of '{tool_name}' tool."
