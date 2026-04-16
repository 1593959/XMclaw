"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_41ded206"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        bash_history = kwargs.get("bash_history", [])
        limit = kwargs.get("limit", 10)
        
        if not bash_history:
            return "No bash command history available for analysis."
        
        recent_commands = bash_history[:limit]
        command_counts = {}
        command_patterns = []
        
        for cmd in recent_commands:
            if cmd and isinstance(cmd, str):
                parts = cmd.strip().split()
                if parts:
                    base_cmd = parts[0]
                    command_counts[base_cmd] = command_counts.get(base_cmd, 0) + 1
        
        sorted_commands = sorted(command_counts.items(), key=lambda x: x[1], reverse=True)
        
        analysis = f"Frequent Bash Usage Analysis (Last {len(recent_commands)} commands):\n"
        analysis += "=" * 50 + "\n\n"
        
        analysis += "Command Frequency:\n"
        for cmd, count in sorted_commands:
            bar = "█" * count
            percentage = (count / len(recent_commands)) * 100
            analysis += f"  {cmd:<15} {bar:<10} {count} times ({percentage:.0f}%)\n"
        
        analysis += "\n" + "=" * 50 + "\n"
        analysis += f"Total unique commands: {len(command_counts)}\n"
        analysis += f"Most frequent: {sorted_commands[0][0]} ({sorted_commands[0][1]} times)\n"
        
        suggestions = []
        for cmd, count in sorted_commands:
            if count >= 3:
                if cmd in ["cd", "ls", "grep", "find"]:
                    suggestions.append(f"Consider creating an alias for '{cmd}' to speed up navigation")
                elif cmd == "docker" and any("run" in bash_history[i] for i in range(len(bash_history[:limit]))):
                    suggestions.append("Docker commands detected - consider using docker-compose for common workflows")
        
        if suggestions:
            analysis += "\nOptimization Suggestions:\n"
            for i, suggestion in enumerate(suggestions, 1):
                analysis += f"  {i}. {suggestion}\n"
        
        return analysis
