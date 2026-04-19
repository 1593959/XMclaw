"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_196dac3e"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        import subprocess
        import shlex
        
        def execute(command, shell=True):
            if isinstance(command, str):
                if shell:
                    cmd = command
                else:
                    cmd = shlex.split(command)
            else:
                cmd = command
        
            try:
                result = subprocess.run(
                    cmd,
                    shell=shell,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
        
                output = {
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'returncode': result.returncode,
                    'success': result.returncode == 0
                }
        
                if result.returncode != 0:
                    return {'error': f'Command failed with exit code {result.returncode}', 'details': output}
        
                return {'result': output['stdout'], 'success': True}
        
            except subprocess.TimeoutExpired:
                return {'error': 'Command timed out after 30 seconds', 'success': False}
            except Exception as e:
                return {'error': str(e), 'success': False}
