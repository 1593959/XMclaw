"""Skill registry for dynamic skill invocation."""
from xmclaw.skills.manager import SkillManager


class SkillRegistry:
    def __init__(self):
        self._manager = SkillManager()
        self._manager.load_all()

    async def execute(self, skill_name: str, arguments: dict) -> str:
        skill = self._manager.get(skill_name)
        if not skill:
            # Try by name match
            for sid, s in self._manager.skills.items():
                if s.get("name") == skill_name:
                    skill = s
                    break
        if not skill:
            return f"Skill '{skill_name}' not found. Available: {list(self._manager.skills.keys())}"

        # For now, skills are declarative; execution is a no-op placeholder.
        # In the future, skills can map to Python callables or shell commands.
        return f"Executed skill '{skill_name}' with args {arguments}"
