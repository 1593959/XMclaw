"""Skill invocation tool - call registered skills dynamically."""
from xmclaw.tools.base import Tool
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
        # Skills are loaded by the orchestrator; we resolve via the skill registry
        try:
            from xmclaw.skills.registry import SkillRegistry
            registry = SkillRegistry()
            result = await registry.execute(skill_name, args)
            return f"[Skill {skill_name}] {result}"
        except Exception as e:
            return f"[Skill Error: {e}]"
