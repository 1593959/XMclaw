"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_91593c86"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        command = context.get('command', '')
        self.logger.info(f'Bash command executed: {command}')
        # Maintain a limited history of recent commands
        history = context.get('history', [])
        history.append(command)
        context['history'] = history[-10:]
        # Detect repeated commands
        if history.count(command) > 3:
            suggestion = "Alias suggestion: consider creating an alias for '" + command + "'"
            context['suggestion'] = suggestion
        # Return enriched context
        return context
