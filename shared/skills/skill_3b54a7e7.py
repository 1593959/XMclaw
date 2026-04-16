"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_3b54a7e7"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        recent_commands = []
        for entry in context.get('tool_history', []):
            if entry.get('tool') == 'bash':
                recent_commands.append(entry['command'])
        # Keep the most recent up to 10 commands
        recent_commands = recent_commands[-10:]
        # Deduplicate preserving order
        seen = set()
        unique_commands = []
        for cmd in recent_commands:
            if cmd not in seen:
                seen.add(cmd)
                unique_commands.append(cmd)
        # Write commands to a shell script
        script_path = '/tmp/recent_bash_script.sh'
        with open(script_path, 'w') as f:
            f.write('#!/bin/bash\n')
            for cmd in unique_commands:
                f.write(cmd + '\n')
        import os
        os.chmod(script_path, 0o755)
        return {'script_path': script_path}
