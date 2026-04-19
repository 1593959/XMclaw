"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_7c936efd"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        recent_commands = context.get_recent_bash_commands(10)
        script_lines = ['#!/bin/bash'] + recent_commands
        script_content = '\\n'.join(script_lines)
        script_path = '/tmp/recent_commands.sh'
        with open(script_path, 'w') as f:
            f.write(script_content)
        os.chmod(script_path, 0o755)
        context.offer_to_run(script_path)
