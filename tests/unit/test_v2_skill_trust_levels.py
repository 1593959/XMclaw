"""Epic #27 P2 G-06 (2026-05-19) — SkillTrustLevel enum, loader trust
assignment, ``promote()`` dangerous-verdict gate.

Pins:
  * Enum values match the wire-string contract (``"user"``, etc).
  * Default trust on bare SkillManifest is USER.
  * Spec description renders the trust tag in the trailer.
  * promote() refuses when evidence contains ``dangerous:`` markers
    unless force=True; raises DangerousPromotionError specifically.
  * UserSkillsLoader._trust_for + _read_installed_skill_ids handle
    missing / malformed / valid marketplace registries; presence in
    the registry overrides USER → INSTALLED.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest, SkillTrustLevel
from xmclaw.skills.registry import (
    DangerousPromotionError,
    SkillRegistry,
)
from xmclaw.skills.tool_bridge import (
    DISCLOSURE_MODE_INLINE,
    SkillToolProvider,
)
from xmclaw.skills.user_loader import UserSkillsLoader


# ── enum + manifest default ───────────────────────────────────────


def test_trust_level_enum_wire_values() -> None:
    """The Enum is string-valued so manifest authors can write
    ``trust_level: user`` in YAML and round-trip without bespoke
    serialisation."""
    assert SkillTrustLevel.UNTRUSTED.value == "untrusted"
    assert SkillTrustLevel.INSTALLED.value == "installed"
    assert SkillTrustLevel.USER.value == "user"
    assert SkillTrustLevel.BUILTIN.value == "builtin"


def test_manifest_default_trust_is_user() -> None:
    """Existing tests + legacy manifests that don't set trust_level
    get USER by default (preserves backward compat)."""
    m = SkillManifest(id="x", version=1)
    assert m.trust_level == SkillTrustLevel.USER


def test_manifest_explicit_trust_round_trips() -> None:
    m = SkillManifest(
        id="x", version=1, trust_level=SkillTrustLevel.INSTALLED,
    )
    assert m.trust_level == SkillTrustLevel.INSTALLED


# ── spec description includes trust ───────────────────────────────


class _NoopSkill(Skill):
    def __init__(self, sid: str, version: int = 1) -> None:
        self.id = sid
        self.version = version

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result="ok", side_effects=[])


def _register_two_versions(reg: SkillRegistry, sid: str) -> None:
    """Register v1 and v2 of ``sid`` with matching manifests so
    ``promote()`` has an actual target."""
    reg.register(
        _NoopSkill(sid, 1), SkillManifest(id=sid, version=1),
    )
    reg.register(
        _NoopSkill(sid, 2), SkillManifest(id=sid, version=2),
        set_head=False,
    )


def test_spec_description_carries_trust_tag() -> None:
    reg = SkillRegistry()
    reg.register(
        _NoopSkill("user-skill"),
        SkillManifest(
            id="user-skill", version=1,
            description="Does a thing",
            trust_level=SkillTrustLevel.USER,
        ),
    )
    reg.register(
        _NoopSkill("market-skill"),
        SkillManifest(
            id="market-skill", version=1,
            description="From marketplace",
            trust_level=SkillTrustLevel.INSTALLED,
        ),
    )
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    specs = {s.name: s for s in bridge.list_tools()}
    assert "trust=user" in specs["skill_user-skill"].description
    assert "trust=installed" in specs["skill_market-skill"].description


def test_spec_description_renders_allowed_tools_preview() -> None:
    """allowed_tools surfacing in description so LLM has signal even
    if it doesn't read skill_view."""
    reg = SkillRegistry()
    reg.register(
        _NoopSkill("restricted-skill"),
        SkillManifest(
            id="restricted-skill", version=1,
            allowed_tools=("file_read", "bash"),
            trust_level=SkillTrustLevel.INSTALLED,
        ),
    )
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    spec = next(
        s for s in bridge.list_tools()
        if s.name == "skill_restricted-skill"
    )
    assert "allowed_tools:" in spec.description
    assert "file_read" in spec.description
    assert "bash" in spec.description


def test_spec_description_truncates_long_allowed_tools_list() -> None:
    reg = SkillRegistry()
    reg.register(
        _NoopSkill("many"),
        SkillManifest(
            id="many", version=1,
            allowed_tools=tuple(f"tool_{i}" for i in range(10)),
        ),
    )
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    spec = next(s for s in bridge.list_tools() if s.name == "skill_many")
    # Should show first 5 + "(+5 more)".
    assert "tool_0" in spec.description
    assert "tool_4" in spec.description
    assert "+5 more" in spec.description
    # And NOT show tool_9 inline.
    assert "tool_9" not in spec.description


# ── promote() dangerous-verdict gate ──────────────────────────────


def test_promote_refuses_dangerous_evidence_without_force() -> None:
    reg = SkillRegistry()
    _register_two_versions(reg, "foo")
    with pytest.raises(DangerousPromotionError) as exc_info:
        reg.promote("foo", 2, evidence=["dangerous: invokes rm -rf"])
    assert "dangerous" in str(exc_info.value).lower()
    # HEAD should NOT have advanced.
    assert reg.active_version("foo") == 1


def test_promote_allows_dangerous_evidence_with_force() -> None:
    reg = SkillRegistry()
    _register_two_versions(reg, "foo")
    # force=True bypasses the gate — but the audit record still
    # shows the dangerous evidence the caller saw + accepted.
    rec = reg.promote(
        "foo", 2,
        evidence=["dangerous: invokes rm -rf"],
        force=True,
    )
    assert rec.to_version == 2
    assert reg.active_version("foo") == 2


def test_promote_alt_phrasing_verdict_equals_dangerous() -> None:
    """Match both ``dangerous:`` (grader-stamped) and
    ``verdict=dangerous`` (eval-runner-stamped) evidence formats."""
    reg = SkillRegistry()
    _register_two_versions(reg, "foo")
    with pytest.raises(DangerousPromotionError):
        reg.promote("foo", 2, evidence=["verdict=dangerous, score=0.1"])


def test_promote_non_dangerous_evidence_passes() -> None:
    """Plain evidence without the danger marker still flows through."""
    reg = SkillRegistry()
    _register_two_versions(reg, "foo")
    rec = reg.promote(
        "foo", 2, evidence=["bench:1.12x", "grader: safe"],
    )
    assert rec.to_version == 2


def test_promote_evidence_case_insensitive_match() -> None:
    """Evidence comparison is case-insensitive — ``DANGEROUS:`` and
    ``Dangerous:`` should hit the gate same as ``dangerous:``."""
    reg = SkillRegistry()
    _register_two_versions(reg, "foo")
    with pytest.raises(DangerousPromotionError):
        reg.promote("foo", 2, evidence=["DANGEROUS: shell exec"])


# ── UserSkillsLoader trust assignment ─────────────────────────────


def test_loader_trust_for_user_authored_skill(tmp_path: Path) -> None:
    """No marketplace registry → every skill_id maps to USER trust."""
    nonexistent = tmp_path / "no-marketplace.json"
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=nonexistent,
    ):
        loader = UserSkillsLoader(SkillRegistry(), tmp_path)
    assert loader._trust_for("my-user-skill") == SkillTrustLevel.USER
    assert loader._trust_for("anything") == SkillTrustLevel.USER


def test_loader_reads_installed_skills_from_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``.marketplace.json`` lists a skill, the loader tags it
    INSTALLED — overriding the manifest author's claimed trust."""
    # Stand up a fake marketplace registry at the expected path.
    market_dir = tmp_path / "skills_user"
    market_dir.mkdir()
    registry_file = market_dir / ".marketplace.json"
    registry_file.write_text(json.dumps({
        "skills": [
            {"id": "market-skill", "version": "1", "source": "github:foo/bar"},
            {"id": "another-market", "version": "2", "source": "git+..."},
        ],
    }), encoding="utf-8")

    # Patch installed_registry_path to return our test file. Honor
    # both common import paths to be defensive about cache state.
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=registry_file,
    ):
        loader = UserSkillsLoader(
            registry=SkillRegistry(), skills_root=market_dir,
        )
    assert loader._trust_for("market-skill") == SkillTrustLevel.INSTALLED
    assert loader._trust_for("another-market") == SkillTrustLevel.INSTALLED
    # Unlisted IDs still default to USER.
    assert loader._trust_for("user-only") == SkillTrustLevel.USER


def test_loader_handles_missing_marketplace_registry(
    tmp_path: Path,
) -> None:
    """Daemon boot must not crash when ``.marketplace.json`` doesn't
    exist — the empty set is a legitimate state for a fresh user."""
    nonexistent = tmp_path / "skills_user" / ".marketplace.json"
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=nonexistent,
    ):
        loader = UserSkillsLoader(
            registry=SkillRegistry(), skills_root=tmp_path,
        )
    assert loader._installed_skill_ids == frozenset()


def test_loader_handles_corrupt_marketplace_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken JSON shouldn't crash the loader — degrade to empty set
    (the worst case: marketplace-installed skills temporarily lose
    their INSTALLED tag, never crash)."""
    market_dir = tmp_path / "skills_user"
    market_dir.mkdir()
    registry_file = market_dir / ".marketplace.json"
    registry_file.write_text("not valid json {{{", encoding="utf-8")
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=registry_file,
    ):
        loader = UserSkillsLoader(
            registry=SkillRegistry(), skills_root=market_dir,
        )
    assert loader._installed_skill_ids == frozenset()
    assert loader._trust_for("anything") == SkillTrustLevel.USER


def test_loader_handles_marketplace_wrong_shape(
    tmp_path: Path,
) -> None:
    """A registry with ``skills`` being a string / int / null
    shouldn't crash — degrade to empty set."""
    market_dir = tmp_path / "skills_user"
    market_dir.mkdir()
    registry_file = market_dir / ".marketplace.json"
    registry_file.write_text(json.dumps({"skills": "not-a-list"}), encoding="utf-8")
    with patch(
        "xmclaw.skills.marketplace.installed_registry_path",
        return_value=registry_file,
    ):
        loader = UserSkillsLoader(
            registry=SkillRegistry(), skills_root=market_dir,
        )
    assert loader._installed_skill_ids == frozenset()
