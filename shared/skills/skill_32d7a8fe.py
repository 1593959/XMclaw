"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_32d7a8fe"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
            # Retrieve recent bash commands from history
            recent_commands = context.get_recent_tool_calls('bash')
            # Deduplicate preserving order
            unique_commands = list(dict.fromkeys(recent_commands))
            # Combine into a script
            script_content = '#!/bin/bash
        ' + '
        '.join(unique_commands)
            # Write script to file
            script_path = 'combined_bash_script.sh'
            with open(script_path, 'w') as f:
                f.write(script_content)
            # Make script executable
            import os
            os.chmod(script_path, 0o755)
            # Notify the user
            context.notify(f'Created executable script: {script_path}')
