"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_5ce9db23"
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
        
        def execute(self, command):
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True
            )
        
            if result.returncode == 0:
                return {
                    "success": True,
                    "output": result.stdout,
                    "errors": result.stderr
                }
            else:
                return {
                    "success": False,
                    "output": result.stdout,
                    "errors": result.stderr,
                    "return_code": result.returncode
                }
