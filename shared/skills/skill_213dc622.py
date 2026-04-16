"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_213dc622"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Retrieve the last N bash commands from the history
        recent_cmds = self.history.get_last(10)
        # Analyze patterns for repetition
        patterns = self.analyzer.detect_repetitive_commands(recent_cmds)
        # If patterns found, offer alias suggestions
        if patterns:
            self.present_suggestions(patterns)
        else:
            # No obvious pattern; simply run the command
            self.run_bash(self.user_input)
