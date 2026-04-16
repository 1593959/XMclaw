"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_577d569d"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        recent_commands = kwargs.get("recent_commands", [])
        command_history = kwargs.get("command_history", [])
        analysis_depth = kwargs.get("analysis_depth", "basic")
        
        from collections import Counter
        
        all_commands = recent_commands + command_history
        if not all_commands:
            return "No bash command history available for analysis."
        
        command_counts = Counter(all_commands)
        top_commands = command_counts.most_common(10)
        
        result = "=== Frequent Bash Command Analysis ===\n\n"
        result += "Top 10 most used commands:\n"
        
        for i, (cmd, count) in enumerate(top_commands, 1):
            bar = "█" * min(count, 20)
            result += f"{i}. {cmd[:60]:<60} {count:>3} {bar}\n"
        
        frequent_threshold = 3
        repeated_patterns = [(cmd, count) for cmd, count in top_commands if count >= frequent_threshold]
        
        if repeated_patterns:
            result += "\n⚠️ Repeated Commands (potential automation candidates):\n"
            for cmd, count in repeated_patterns:
                result += f"  • '{cmd}' - used {count} times\n"
                if count >= 5:
                    result += f"    Consider creating an alias or script for this command\n"
        
        if analysis_depth == "detailed":
            result += "\n📊 Usage Statistics:\n"
            total_commands = sum(command_counts.values())
            unique_commands = len(command_counts)
            result += f"  Total commands: {total_commands}\n"
            result += f"  Unique commands: {unique_commands}\n"
            result += f"  Repetition rate: {(1 - unique_commands/total_commands)*100:.1f}%\n"
        
        return result
