"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_ec70d8a2"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Analyze recent bash commands
        recent_commands = context.get('recent_bash_commands', [])
        from collections import Counter
        counter = Counter(recent_commands)
        top_command, count = counter.most_common(1)[0] if counter else (None, 0)
        if count >= 3:
            suggestion = f'Consider creating a script for the frequently used command: {top_command}'
        else:
            suggestion = 'Your bash usage is moderate. Keep exploring!'
        context.setdefault('suggestions', []).append(suggestion)
        return context
