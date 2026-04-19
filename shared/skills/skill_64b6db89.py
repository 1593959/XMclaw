"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_64b6db89"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        history = kwargs.get("history", [])
        max_results = kwargs.get("max_results", 10)
        
        # Count frequency of each base command
        command_counts = {}
        for cmd in history:
            parts = cmd.strip().split()
            if parts:
                base_cmd = parts[0]
                command_counts[base_cmd] = command_counts.get(base_cmd, 0) + 1
        
        # Sort by frequency and get top commands
        top_commands = sorted(command_counts.items(), key=lambda x: x[1], reverse=True)[:max_results]
        
        if top_commands:
            result_lines = ["Auto-detected frequent bash commands:", ""]
            for i, (cmd, count) in enumerate(top_commands, 1):
                result_lines.append(f"{i}. `{cmd}` - used {count} times")
            return "\n".join(result_lines)
        else:
            return "No bash command history available for analysis."
