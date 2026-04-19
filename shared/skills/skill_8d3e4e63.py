"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_8d3e4e63"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        # Retrieve recent bash commands from the context
        recent_commands = context.get('recent_bash_commands', [])
        if len(recent_commands) < 5:
            return {'status': 'skip', 'message': 'Not enough bash usage detected.'}
        
        # Analyze command frequencies
        from collections import Counter
        freq_counter = Counter(recent_commands)
        top_commands = freq_counter.most_common(3)
        
        # Build a consolidated script
        script_lines = ['#!/bin/bash', '# Auto-generated script based on recent bash usage']
        for cmd, count in top_commands:
            script_lines.append(f'# Command used {count} times')
            script_lines.append(cmd)
        generated_script = '\\n'.join(script_lines)
        
        return {'status': 'success', 'generated_script': generated_script}
