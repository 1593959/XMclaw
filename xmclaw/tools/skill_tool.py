"""Skill invocation tool - call registered skills dynamically."""
from xmclaw.tools.base import Tool
from xmclaw.tools.registry import ToolRegistry
from xmclaw.utils.log import logger


class SkillTool(Tool):
    name = "skill"
    description = "Invoke a registered skill by name with the given arguments."
    parameters = {
        "skill_name": {
            "type": "string",
            "description": "Name of the skill to invoke.",
        },
        "arguments": {
            "type": "object",
            "description": "Arguments to pass to the skill.",
        },
    }

    async def execute(self, skill_name: str, arguments: dict | None = None) -> str:
        args = arguments or {}
        logger.info("skill_tool_invoke", skill_name=skill_name, args=args)
        try:
            registry = ToolRegistry.get_shared()
            if registry is None:
                return "[Skill Error: Tool registry not initialized]"
            # Skills are loaded as tools with the "skill_" prefix in ToolRegistry._tools
            tool = registry._tools.get(f"skill_{skill_name}")
            if tool is None:
                available = [n for n in registry._tools.keys() if n.startswith("skill_")]
                return f"[Skill '{skill_name}' not found. Available: {available[:10]}]"
            result = await tool.execute(**args)
            return f"[Skill {skill_name}] {result}"
        except Exception as e:
            return f"[Skill Error: {e}]"
