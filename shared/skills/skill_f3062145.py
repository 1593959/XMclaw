"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_f3062145"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        bash_history = []
        repetition_threshold = 3
        optimization_tips = []
        
        # Collect recent bash invocations from context
        recent_commands = context.get('bash_history', [])
        bash_history.extend(recent_commands)
        
        # Detect repetitive commands
        from collections import Counter
        command_counts = Counter(bash_history)
        repetitive_commands = [
            cmd for cmd, count in command_counts.items() if count >= repetition_threshold
        ]
        
        if repetitive_commands:
            script_lines = ['#!/bin/bash', '# Auto-generated reusable script', '']
            for cmd in repetitive_commands:
                script_lines.append(f'# Repeated command: {cmd}')
                script_lines.append(cmd)
                script_lines.append('')
            suggested_script = '\n'.join(script_lines)
            optimization_tips.append(
                f'Detected {len(repetitive_commands)} repetitive command(s). '
                f'Consider consolidating into a script:\n{suggested_script}'
            )
        
        # Check for complex piped commands and suggest aliases
        for cmd in bash_history:
            if cmd.count('|') >= 2 and cmd not in [tip for tip in optimization_tips]:
                optimization_tips.append(
                    f'Complex pipe detected: `{cmd}`\n'
                    f'Consider creating an alias: alias shortcmd=\'{cmd}\''
                )
        
        # Build response
        if optimization_tips:
            response = 'BashPowerUser Gene activated:\n\n'
            for i, tip in enumerate(optimization_tips, 1):
                response += f'{i}. {tip}\n\n'
        else:
            response = (
                'BashPowerUser Gene: Monitoring your bash usage. '
                'No repetitive patterns detected yet. Keep working and I will '
                'suggest optimizations as patterns emerge.'
            )
        
        context['bash_power_user_suggestions'] = optimization_tips
        return response
