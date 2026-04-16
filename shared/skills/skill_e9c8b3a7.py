"""
Tool 'bash' was used 10 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentBashUsage(Tool):
    name = "skill_e9c8b3a7"
    description = """Tool 'bash' was used 10 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        command = command.strip()
        if not command:
            raise ValueError("Command cannot be empty")
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Command failed: {result.stderr}")
        return result.stdout, result.stderr
