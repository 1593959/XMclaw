"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_6caa7063"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        usage = context.get('usage', [])
        recent_commands = [cmd for cmd in usage if cmd.get('tool') == 'bash']
        command_counts = {}
        for cmd in recent_commands:
            cmd_str = cmd.get('command', '')
            command_counts[cmd_str] = command_counts.get(cmd_str, 0) + 1
        
        top_commands = sorted(command_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        insights = []
        if len(recent_commands) > 10:
            insights.append('You have executed bash 10 times recently.')
            insights.append('Most frequent commands:')
            for cmd, count in top_commands:
                insights.append(f'  {cmd}: {count} times')
            if any('grep' in cmd for cmd, _ in top_commands):
                insights.append('Consider using find -exec grep or ripgrep for faster searching.')
            if any('curl' in cmd for cmd, _ in top_commands):
                insights.append('You can combine multiple curl calls into a script or use xargs.')
        context['insights'] = insights
