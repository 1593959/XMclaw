"""Quick test for manifest.json permissions_enforced bug."""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import UserSkillsLoader

with tempfile.TemporaryDirectory() as tmpdir:
    manifest_path = Path(tmpdir) / "manifest.json"
    manifest_path.write_text(json.dumps({
        "id": "test",
        "version": 1,
        "permissions_enforced": True,
        "trust_level": "builtin",
    }), encoding="utf-8")

    registry = SkillRegistry()
    loader = UserSkillsLoader(registry, Path(tmpdir))
    manifest = loader._load_manifest(manifest_path, "test", 1)

    print(f"permissions_enforced: {manifest.permissions_enforced!r}")
    print(f"trust_level: {manifest.trust_level!r}")
    print(f"Expected permissions_enforced=True, got {manifest.permissions_enforced}")
    print(f"Expected trust_level=builtin, got {manifest.trust_level}")
