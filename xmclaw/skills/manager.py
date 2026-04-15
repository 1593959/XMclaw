"""Skill manager: load, execute, and evolve skills."""
import json
from pathlib import Path
from typing import Any

from xmclaw.utils.paths import BASE_DIR


class SkillManager:
    def __init__(self):
        self.skills: dict[str, dict] = {}
        self.shared_dir = BASE_DIR / "shared" / "skills"

    def load_all(self) -> None:
        self.skills = {}
        for path in self.shared_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.skills[data["id"]] = data

    def get(self, skill_id: str) -> dict[str, Any] | None:
        return self.skills.get(skill_id)

    def list_skills(self) -> list[dict[str, Any]]:
        return list(self.skills.values())
