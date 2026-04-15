"""
Tool 'bash' was used 19 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_e122580c"
    description = """Tool 'bash' was used 19 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Retrieve recent tool usage history to analyze bash commands
        tool_history = kwargs.get("tool_history", [])
        # Filter for bash tool invocations from the recent history
        bash_invocations = [t for t in tool_history if t.get("tool") == "bash"][-20:]
        if not bash_invocations:
            return "No recent bash commands found in history."
        # Extract command names from each bash invocation
        recent_commands = []
        for invocation in bash_invocations:
            raw_input = invocation.get("input", "")
            if raw_input:
                # Parse the command (first token is typically the command name)
                parts = raw_input.strip().split()
                if parts:
                    recent_commands.append(parts[0])
        # Analyze frequency of commands
        from collections import Counter
        frequency_analysis = Counter(recent_commands)
        # Format the results into a readable summary
        output_lines = [f"Recent bash usage summary ({len(bash_invocations)} commands analyzed):"]
        output_lines.append("")
        output_lines.append("Most frequently used commands:")
        for command, count in frequency_analysis.most_common(5):
            percentage = (count / len(bash_invocations)) * 100
            output_lines.append(f"  - {command}: {count} times ({percentage:.1f}%)")
        output_lines.append("")
        output_lines.append("Suggestions:")
        if frequency_analysis.most_common(1)[0][1] >= 5:
            output_lines.append("  Consider creating a shell alias for frequently repeated commands.")
        return "\n".join(output_lines)
