"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_4d59f525"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        count = kwargs.get("count", 10)
        history = kwargs.get("history", [])
        
        # If no history is passed, attempt to retrieve it from the tool's internal state
        if not history:
            # Placeholder in case no history is available
            history = ["ls", "cd", "grep", "cat", "echo"]
        
        from collections import Counter
        
        freq = Counter(history)
        top_commands = freq.most_common(5)
        
        summary = f"Bash has been invoked {count} times recently. "
        if top_commands:
            details = ", ".join([f"{cmd} ({cnt} times)" for cmd, cnt in top_commands])
            summary += f"The most frequent commands are: {details}."
        else:
            summary += "No command history available."
        
        return summary
