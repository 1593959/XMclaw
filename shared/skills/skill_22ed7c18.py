"""
Tool 'file_read' was used 5 times recently.
Auto-generated Skill for XMclaw.
"""
from xmclaw.tools.base import Tool

class AutoFrequentFileReadUsage(Tool):
    name = "skill_22ed7c18"
    description = """Tool 'file_read' was used 5 times recently."""
    parameters = {
    "input": {
        "type": "string",
        "description": "Input for the skill"
    }
}

    async def execute(self, **kwargs) -> str:
        """Execute the skill."""
        import asyncio
        
        def _sync_read(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        
        file_paths = kwargs.get("file_paths", [])
        if file_paths:
            results = []
            for fp in file_paths:
                try:
                    content = await asyncio.to_thread(_sync_read, fp)
                    results.append(f"{fp}:\n{content}")
                except Exception as e:
                    results.append(f"{fp}: Error - {e}")
            return "\n\n".join(results)
        
        file_path = kwargs.get("path", "")
        if not file_path:
            return "Error: No file path provided."
        
        try:
            content = await asyncio.to_thread(_sync_read, file_path)
            return f"Content of {file_path}:\n{content}"
        except Exception as e:
            return f"Error reading file {file_path}: {e}"
