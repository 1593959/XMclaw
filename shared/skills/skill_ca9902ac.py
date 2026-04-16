"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_ca9902ac"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        recent = context.get('recent_bash_commands', [])
        from collections import Counter
        counter = Counter(recent)
        suggestions = []
        for cmd, count in counter.most_common(5):
            if count >= 3:
                suggestions.append('Consider creating an alias for: ' + cmd)
        return {'suggestions': suggestions}
