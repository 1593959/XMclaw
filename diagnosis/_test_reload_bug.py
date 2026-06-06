"""Quick test for reload_one AttributeError bug."""
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import UserSkillsLoader


class NoIdSkill(Skill):
    # id is missing!
    version = 1
    async def run(self, inp):
        return SkillOutput(ok=True, result="", side_effects=[])


with tempfile.TemporaryDirectory() as tmpdir:
    skills_root = Path(tmpdir) / "skills"
    skills_root.mkdir()
    skill_dir = skills_root / "noid"
    skill_dir.mkdir()
    (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class NoIdSkill(Skill):
    version = 1
    async def run(self, inp):
        return SkillOutput(ok=True, result="", side_effects=[])
""", encoding="utf-8")

    registry = SkillRegistry()
    loader = UserSkillsLoader(registry, skills_root)

    print("Testing _load_one with missing id...")
    try:
        results = loader.load_all()
        for r in results:
            print(f"  {r}")
    except Exception as e:
        print(f"  UNCAUGHT: {type(e).__name__}: {e}")

    print("\nTesting reload_one with missing id...")
    try:
        inst, manifest, err = loader.reload_one(skill_dir)
        print(f"  inst={inst}, manifest={manifest}, err={err!r}")
    except Exception as e:
        print(f"  UNCAUGHT: {type(e).__name__}: {e}")
