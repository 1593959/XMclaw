"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_505fbdea"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        recent_commands = context.get('recent_bash_commands', [])
        if not recent_commands:
            return {'suggestions': []}
        from collections import Counter
        counter = Counter(recent_commands)
        top = counter.most_common(3)
        suggestions = []
        for cmd, count in top:
            if count > 1:
                alias_name = f"alias_{cmd.replace(' ', '_').replace('/', '_')}"
                suggestions.append({'alias_name': alias_name, 'command': cmd})
        return {'suggestions': suggestions}
