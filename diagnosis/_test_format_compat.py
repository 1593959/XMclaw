"""Diagnostic test script for XMclaw skill format compatibility."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure xmclaw is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xmclaw.skills.base import Skill
from xmclaw.skills.manifest import SkillManifest, SkillTrustLevel
from xmclaw.skills.markdown_skill import MarkdownProcedureSkill
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.user_loader import (
    UserSkillsLoader,
    _parse_skill_md_frontmatter,
    _parse_skill_md_frontmatter_extras,
    _parse_skill_md_created_by,
    _FRONTMATTER_BLOCK_RE,
)


def test_skill_base_class():
    """Test 5: Check Skill base class id/version are class attrs or instance attrs."""
    print("=" * 60)
    print("TEST 5: Skill base class id/version attribute nature")
    print("=" * 60)

    # Skill is an ABC with class attributes id and version
    print(f"Skill.id exists on class? {hasattr(Skill, 'id')}")
    print(f"Skill.version exists on class? {hasattr(Skill, 'version')}")
    print(f"Skill.id value on class: {getattr(Skill, 'id', 'MISSING')!r}")
    print(f"Skill.version value on class: {getattr(Skill, 'version', 'MISSING')!r}")

    # Try to instantiate Skill directly (should fail, it's abstract)
    try:
        Skill()
        print("ERROR: Skill() instantiation did NOT raise (should be abstract)")
    except TypeError as e:
        print(f"OK: Skill() raises TypeError: {e}")

    # Create a concrete subclass
    class MySkill(Skill):
        id = "my_skill"
        version = 1
        async def run(self, inp):
            return None

    inst = MySkill()
    print(f"MySkill instance id: {inst.id!r} (type: {type(inst.id).__name__})")
    print(f"MySkill instance version: {inst.version!r} (type: {type(inst.version).__name__})")

    # Check if id/version are instance attrs or just class attrs
    print(f"'id' in inst.__dict__: {'id' in inst.__dict__}")
    print(f"'version' in inst.__dict__: {'version' in inst.__dict__}")
    print(f"'id' in MySkill.__dict__: {'id' in MySkill.__dict__}")
    print(f"'version' in MySkill.__dict__: {'version' in MySkill.__dict__}")

    # Check dataclass subclass
    class DataSkill(Skill):
        id: str = "data_skill"
        version: int = 1
        async def run(self, inp):
            return None

    # Is it a dataclass?
    print(f"MySkill is dataclass? {hasattr(MySkill, '__dataclass_fields__')}")
    print(f"DataSkill is dataclass? {hasattr(DataSkill, '__dataclass_fields__')}")

    # What about MarkdownProcedureSkill?
    md_skill = MarkdownProcedureSkill(id="md_test", body="hello", version=2)
    print(f"MarkdownProcedureSkill id: {md_skill.id!r}")
    print(f"MarkdownProcedureSkill version: {md_skill.version!r}")
    print(f"'id' in md_skill.__dict__: {'id' in md_skill.__dict__}")
    print(f"'version' in md_skill.__dict__: {'version' in md_skill.__dict__}")
    print()


def test_skill_md_frontmatter():
    """Test 1a: Parse standard SKILL.md frontmatter."""
    print("=" * 60)
    print("TEST 1a: SKILL.md frontmatter parsing")
    print("=" * 60)

    skill_md_content = """---
name: Test Skill
description: A test skill for diagnostics
triggers: [test, diagnostic, format]
when_to_use: Use this when testing format compatibility
allowed_tools: [bash, file_read]
paths: [src/**/*.py]
requires_restart: false
model: opus
created_by: user
---

# Test Skill

This is the body of the test skill.

## Step 1

Do something useful.
"""

    title, description, triggers = _parse_skill_md_frontmatter(skill_md_content)
    print(f"title: {title!r}")
    print(f"description: {description!r}")
    print(f"triggers: {triggers!r}")

    extras = _parse_skill_md_frontmatter_extras(skill_md_content)
    print(f"extras: {extras!r}")

    created_by = _parse_skill_md_created_by(skill_md_content)
    print(f"created_by: {created_by!r}")

    # Test edge cases
    print("\n--- Edge case: bracketed list with spaces ---")
    bracket_md = """---
triggers: [ a , b , c ]
---
"""
    t, d, tr = _parse_skill_md_frontmatter(bracket_md)
    print(f"triggers from spaced bracket list: {tr!r}")

    print("\n--- Edge case: no frontmatter ---")
    no_fm = "# Title\n\nSome description here."
    t, d, tr = _parse_skill_md_frontmatter(no_fm)
    print(f"title (fallback): {t!r}")
    print(f"description (fallback): {d!r}")

    print("\n--- Edge case: hyphenated keys ---")
    hyphen_md = """---
when-to-use: Use when foo
allowed-tools: [bash, file_write]
requires-restart: true
---
"""
    extras = _parse_skill_md_frontmatter_extras(hyphen_md)
    print(f"extras from hyphenated keys: {extras!r}")
    print()


def test_user_skills_loader_markdown():
    """Test 1b: Load a SKILL.md via UserSkillsLoader."""
    print("=" * 60)
    print("TEST 1b: UserSkillsLoader loading SKILL.md")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_root = Path(tmpdir) / "skills"
        skills_root.mkdir()

        skill_dir = skills_root / "test_md_skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: Markdown Test Skill
description: Testing markdown skill loading
triggers: [md, test]
when_to_use: Use when testing markdown skills
allowed_tools: [bash]
---

# Markdown Test Skill

Run diagnostics.
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()

        print(f"Load results: {results}")
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, kind={r.kind}, error={r.error!r}")

        # Check registry
        print(f"Registry list_skill_ids: {registry.list_skill_ids()}")
        if registry.list_skill_ids():
            sid = registry.list_skill_ids()[0]
            skill = registry.get(sid)
            manifest = registry.ref(sid).manifest
            print(f"Loaded skill id={skill.id!r}, version={skill.version!r}")
            print(f"Manifest title={manifest.title!r}, description={manifest.description!r}")
            print(f"Manifest triggers={manifest.triggers!r}")
            print(f"Manifest when_to_use={manifest.when_to_use!r}")
            print(f"Manifest allowed_tools={manifest.allowed_tools!r}")
            print(f"Manifest trust_level={manifest.trust_level!r}")
    print()


def test_user_skills_loader_python():
    """Test 2: Load a standard skill.py via UserSkillsLoader."""
    print("=" * 60)
    print("TEST 2: UserSkillsLoader loading skill.py (Python Skill subclass)")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_root = Path(tmpdir) / "skills"
        skills_root.mkdir()

        skill_dir = skills_root / "test_py_skill"
        skill_dir.mkdir()

        skill_py = skill_dir / "skill.py"
        skill_py.write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class TestPySkill(Skill):
    id = "test_py_skill"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="hello", side_effects=[])
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()

        print(f"Load results: {results}")
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, kind={r.kind}, error={r.error!r}")

        print(f"Registry list_skill_ids: {registry.list_skill_ids()}")
        if registry.list_skill_ids():
            sid = registry.list_skill_ids()[0]
            skill = registry.get(sid)
            manifest = registry.ref(sid).manifest
            print(f"Loaded skill id={skill.id!r}, version={skill.version!r}")
            print(f"Manifest id={manifest.id!r}, version={manifest.version!r}")
    print()


def test_manifest_json():
    """Test 3: Parse manifest.json via _load_manifest."""
    print("=" * 60)
    print("TEST 3: manifest.json parsing via _load_manifest")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = Path(tmpdir) / "manifest.json"

        manifest_data = {
            "id": "test_manifest_skill",
            "version": 2,
            "title": "Manifest Test",
            "description": "Testing manifest parsing",
            "permissions_fs": ["/tmp"],
            "permissions_net": ["localhost"],
            "permissions_subprocess": ["git"],
            "max_cpu_seconds": 60.0,
            "max_memory_mb": 1024,
            "created_by": "evolved",
            "evidence": ["bench_passed"],
            "triggers": ["manifest", "test"],
            "when_to_use": "Use when manifest testing",
            "allowed_tools": ["bash", "file_read"],
            "paths": ["tests/**/*.py"],
            "requires_restart": True,
            "model": "opus",
            "trust_level": "installed",
        }
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        # _load_manifest is an instance method on UserSkillsLoader
        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, Path(tmpdir))

        try:
            manifest = loader._load_manifest(manifest_path, "test_manifest_skill", 2)
            print(f"Parsed manifest:")
            print(f"  id={manifest.id!r}")
            print(f"  version={manifest.version!r}")
            print(f"  title={manifest.title!r}")
            print(f"  description={manifest.description!r}")
            print(f"  permissions_fs={manifest.permissions_fs!r}")
            print(f"  permissions_net={manifest.permissions_net!r}")
            print(f"  permissions_subprocess={manifest.permissions_subprocess!r}")
            print(f"  max_cpu_seconds={manifest.max_cpu_seconds!r}")
            print(f"  max_memory_mb={manifest.max_memory_mb!r}")
            print(f"  created_by={manifest.created_by!r}")
            print(f"  evidence={manifest.evidence!r}")
            print(f"  triggers={manifest.triggers!r}")
            print(f"  when_to_use={manifest.when_to_use!r}")
            print(f"  allowed_tools={manifest.allowed_tools!r}")
            print(f"  paths={manifest.paths!r}")
            print(f"  requires_restart={manifest.requires_restart!r}")
            print(f"  model={manifest.model!r}")
            print(f"  trust_level={manifest.trust_level!r}")
        except Exception as e:
            print(f"ERROR loading manifest: {type(e).__name__}: {e}")

        # Test mismatch cases
        print("\n--- Mismatch: id disagrees ---")
        bad_id = Path(tmpdir) / "bad_id.json"
        bad_id.write_text(json.dumps({"id": "wrong_id", "version": 2}), encoding="utf-8")
        try:
            loader._load_manifest(bad_id, "test_manifest_skill", 2)
            print("ERROR: Should have raised on id mismatch")
        except ValueError as e:
            print(f"OK: id mismatch raises ValueError: {e}")

        print("\n--- Mismatch: version disagrees ---")
        bad_ver = Path(tmpdir) / "bad_ver.json"
        bad_ver.write_text(json.dumps({"id": "test_manifest_skill", "version": 99}), encoding="utf-8")
        try:
            loader._load_manifest(bad_ver, "test_manifest_skill", 2)
            print("ERROR: Should have raised on version mismatch")
        except ValueError as e:
            print(f"OK: version mismatch raises ValueError: {e}")

        # Test hyphenated keys
        print("\n--- Hyphenated keys in manifest ---")
        hyphen_path = Path(tmpdir) / "hyphen.json"
        hyphen_path.write_text(json.dumps({
            "whenToUse": "Use when hyphen",
            "allowedTools": ["bash"],
            "requiresRestart": True,
        }), encoding="utf-8")
        manifest = loader._load_manifest(hyphen_path, "hyphen_skill", 1)
        print(f"when_to_use={manifest.when_to_use!r}")
        print(f"allowed_tools={manifest.allowed_tools!r}")
        print(f"requires_restart={manifest.requires_restart!r}")
    print()


def test_manifest_to_dict():
    """Test 4: SkillManifest.to_dict() output."""
    print("=" * 60)
    print("TEST 4: SkillManifest.to_dict() output")
    print("=" * 60)

    manifest = SkillManifest(
        id="test_dict",
        version=1,
        title="Test",
        description="Testing to_dict",
        permissions_fs=("/tmp",),
        permissions_net=(),
        permissions_subprocess=("git",),
        max_cpu_seconds=30.0,
        max_memory_mb=512,
        created_by="user",
        evidence=("pass",),
        triggers=("test",),
        when_to_use="Use when testing",
        allowed_tools=("bash",),
        paths=("src/**/*.py",),
        requires_restart=False,
        model="opus",
        trust_level=SkillTrustLevel.USER,
    )

    d = manifest.to_dict()
    print(f"to_dict() result:")
    for k, v in d.items():
        print(f"  {k}={v!r} (type: {type(v).__name__})")

    # Check tuple -> list conversion
    tuple_fields = ["permissions_fs", "permissions_net", "permissions_subprocess",
                    "evidence", "triggers", "allowed_tools", "paths"]
    for field in tuple_fields:
        val = d[field]
        if isinstance(val, list):
            print(f"OK: {field} is list (was tuple)")
        else:
            print(f"ERROR: {field} is {type(val).__name__}, expected list")

    # Check trust_level serialization
    print(f"trust_level value: {d['trust_level']!r}")
    print(f"trust_level type: {type(d['trust_level']).__name__}")

    # Round-trip check
    print("\n--- Round-trip JSON serialization ---")
    json_str = json.dumps(d)
    print(f"JSON dumps OK: {len(json_str)} chars")
    loaded = json.loads(json_str)
    print(f"JSON loads OK: id={loaded['id']!r}")
    print()


def test_loader_validation_edge_cases():
    """Test edge cases in loader validation logic."""
    print("=" * 60)
    print("TEST 6: Loader validation edge cases")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_root = Path(tmpdir) / "skills"
        skills_root.mkdir()

        # Case 1: skill.py with string version instead of int
        print("\n--- Case 1: version is string '1' instead of int 1 ---")
        skill_dir = skills_root / "str_version_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class StrVerSkill(Skill):
    id = "str_version_skill"
    version = "1"  # String!

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="", side_effects=[])
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, error={r.error!r}")

        # Clean up for next test
        import shutil
        shutil.rmtree(skill_dir)

        # Case 2: skill.py with version = 0 (not positive)
        print("\n--- Case 2: version is 0 (not positive) ---")
        skill_dir = skills_root / "zero_version_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class ZeroVerSkill(Skill):
    id = "zero_version_skill"
    version = 0

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="", side_effects=[])
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, error={r.error!r}")

        shutil.rmtree(skill_dir)

        # Case 3: id mismatch between directory and class
        print("\n--- Case 3: directory name disagrees with Skill.id ---")
        skill_dir = skills_root / "dir_name_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class MismatchSkill(Skill):
    id = "wrong_id"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="", side_effects=[])
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, error={r.error!r}")

        shutil.rmtree(skill_dir)

        # Case 4: build_skill() factory returning non-Skill
        print("\n--- Case 4: build_skill() returns non-Skill ---")
        skill_dir = skills_root / "bad_factory_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("""
def build_skill():
    return "not a skill"
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        try:
            results = loader.load_all()
            for r in results:
                print(f"  skill_id={r.skill_id}, ok={r.ok}, error={r.error!r}")
        except RuntimeError as e:
            print(f"  UNCAUGHT RuntimeError escaped _load_one: {e}")
            print("  THIS IS A BUG: _load_one should catch factory errors, not leak them")

        shutil.rmtree(skill_dir)

        # Case 5: build_skill() factory returning correct Skill
        print("\n--- Case 5: build_skill() returns valid Skill ---")
        skill_dir = skills_root / "good_factory_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class FactorySkill(Skill):
    id = "good_factory_skill"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="factory", side_effects=[])

def build_skill():
    return FactorySkill()
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, error={r.error!r}")
        if registry.list_skill_ids():
            print(f"  Registry has: {registry.list_skill_ids()}")

        shutil.rmtree(skill_dir)

        # Case 6: Both skill.py and SKILL.md exist (skill.py should win)
        print("\n--- Case 6: Both skill.py and SKILL.md exist ---")
        skill_dir = skills_root / "both_formats_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class BothSkill(Skill):
    id = "both_formats_skill"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="python", side_effects=[])
""", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text("""---
name: Markdown Skill
description: This should be ignored
---

# Markdown

This should not load.
""", encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()
        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, kind={r.kind}, error={r.error!r}")
        if registry.list_skill_ids():
            skill = registry.get(registry.list_skill_ids()[0])
            print(f"  Loaded skill type: {type(skill).__name__}")
    print()


def test_frontmatter_parser_bugs():
    """Test specific frontmatter parsing bugs."""
    print("=" * 60)
    print("TEST 7: Frontmatter parser specific bugs")
    print("=" * 60)

    # Bug 1: Multi-line description in frontmatter (not supported per doc)
    print("\n--- Bug 1: Multi-line YAML scalar (folded block) ---")
    folded = """---
description: >
  This is a long description
  that spans multiple lines.
---

# Title
"""
    t, d, tr = _parse_skill_md_frontmatter(folded)
    print(f"title={t!r}, description={d!r}")
    print("NOTE: Parser doc says multi-line scalars are NOT supported")

    # Bug 2: Frontmatter with trailing spaces after ---
    print("\n--- Bug 2: Frontmatter with trailing spaces ---")
    trailing = "---   \nname: Test\n---   \n\n# Title\n"
    t, d, tr = _parse_skill_md_frontmatter(trailing)
    print(f"title={t!r}, description={d!r}")
    print(f"Regex used: {_FRONTMATTER_BLOCK_RE.pattern!r}")

    # Bug 3: Empty frontmatter values
    print("\n--- Bug 3: Empty values ---")
    empty = """---
name:
description:
triggers:
---

# Real Title

Real description.
"""
    t, d, tr = _parse_skill_md_frontmatter(empty)
    print(f"title={t!r}, description={d!r}, triggers={tr!r}")

    # Bug 4: Quoted strings with internal quotes
    print("\n--- Bug 4: Quoted strings ---")
    quoted = """---
name: "Test Skill"
description: 'A test skill'
---
"""
    t, d, tr = _parse_skill_md_frontmatter(quoted)
    print(f"title={t!r}, description={d!r}")

    # Bug 5: created_by regex strictness
    print("\n--- Bug 5: created_by regex edge cases ---")
    from xmclaw.skills.user_loader import _CREATED_BY_RE
    print(f"Regex: {_CREATED_BY_RE.pattern!r}")

    cases = [
        "created_by: evolved",
        "created_by: 'evolved'",
        'created_by: "evolved"',
        "created_by: Evolved",
        "created_by: 123",
        "created_by: evolved-user",
        "created_by: evolved_user",
        "created_by: evolved user",
    ]
    for case in cases:
        m = _CREATED_BY_RE.search(case)
        print(f"  {case!r} -> {m.group(1) if m else None!r}")
    print()


def test_manifest_trust_level_override():
    """Test that loader overrides manifest trust_level."""
    print("=" * 60)
    print("TEST 8: Loader trust_level override")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_root = Path(tmpdir) / "skills"
        skills_root.mkdir()

        skill_dir = skills_root / "trust_test"
        skill_dir.mkdir()

        (skill_dir / "skill.py").write_text("""
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

class TrustSkill(Skill):
    id = "trust_test"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="", side_effects=[])
""", encoding="utf-8")

        (skill_dir / "manifest.json").write_text(json.dumps({
            "trust_level": "builtin",  # Claims builtin!
        }), encoding="utf-8")

        registry = SkillRegistry()
        loader = UserSkillsLoader(registry, skills_root)
        results = loader.load_all()

        for r in results:
            print(f"  skill_id={r.skill_id}, ok={r.ok}, error={r.error!r}")

        if registry.list_skill_ids():
            manifest = registry.ref("trust_test").manifest
            print(f"  Manifest trust_level: {manifest.trust_level!r}")
            print(f"  Expected: 'user' (loader override, not 'builtin')")
    print()


if __name__ == "__main__":
    test_skill_base_class()
    test_skill_md_frontmatter()
    test_user_skills_loader_markdown()
    test_user_skills_loader_python()
    test_manifest_json()
    test_manifest_to_dict()
    test_loader_validation_edge_cases()
    test_frontmatter_parser_bugs()
    test_manifest_trust_level_override()
    print("=" * 60)
    print("ALL TESTS COMPLETE")
    print("=" * 60)
