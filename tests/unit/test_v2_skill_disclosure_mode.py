"""Epic #27 G-04 (2026-05-19) — progressive-disclosure mode tests.

Pins:
  * ``skill_run`` meta-tool is ALWAYS exposed (every mode + 0 skills).
  * ``inline`` mode keeps per-skill ``skill_<id>`` tools surfaced.
  * ``unified`` mode drops per-skill tools but keeps meta-tools.
  * ``auto`` mode flips inline→unified once registered count crosses
    ``unified_threshold``.
  * ``skill_run`` invokes registered skills end-to-end (forward args,
    surface ok/error, route through variant_selector if wired).
  * Bad ``disclosure_mode`` strings fall back to ``auto`` (no crash).
  * ``skill_run`` rejects malformed args structurally + never raises.
  * Prefilter always-on whitelist preserves ``skill_run`` even under
    big libraries where the prefilter trims to top-K.
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.prefilter import select_relevant_skills
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import (
    DISCLOSURE_MODE_AUTO,
    DISCLOSURE_MODE_INLINE,
    DISCLOSURE_MODE_UNIFIED,
    META_BROWSE_TOOL_NAME,
    META_DIFF_TOOL_NAME,
    META_INSTALL_TOOL_NAME,
    META_ROLLBACK_TOOL_NAME,
    META_RUN_TOOL_NAME,
    META_STATUS_TOOL_NAME,
    META_UNINSTALL_TOOL_NAME,
    META_VIEW_TOOL_NAME,
    SkillToolProvider,
)


_META_NAMES = frozenset({
    META_BROWSE_TOOL_NAME,
    META_INSTALL_TOOL_NAME,
    META_UNINSTALL_TOOL_NAME,
    META_STATUS_TOOL_NAME,
    META_VIEW_TOOL_NAME,
    META_RUN_TOOL_NAME,
    # Epic #27 G-07 (2026-05-19): versioned-edit history meta-tools.
    META_DIFF_TOOL_NAME,
    META_ROLLBACK_TOOL_NAME,
})


class _EchoSkill(Skill):
    """Mirror back the args it received."""

    def __init__(self, skill_id: str, *, fail: bool = False) -> None:
        self.id = skill_id
        self.version = 1
        self._fail = fail

    async def run(self, inp: SkillInput) -> SkillOutput:
        if self._fail:
            return SkillOutput(
                ok=False,
                result={"error": "boom"},
                side_effects=[],
            )
        return SkillOutput(
            ok=True,
            result={"echoed": inp.args},
            side_effects=[],
        )


def _registry_with_n(n: int) -> SkillRegistry:
    reg = SkillRegistry()
    for i in range(n):
        sid = f"demo.skill_{i}"
        reg.register(_EchoSkill(sid), SkillManifest(id=sid, version=1))
    return reg


# ── always-on skill_run ────────────────────────────────────────────


def test_skill_run_always_exposed_empty_registry() -> None:
    """Even with zero registered skills the meta-tools (incl. skill_run)
    are surfaced — the LLM needs the discovery affordance regardless."""
    bridge = SkillToolProvider(SkillRegistry())
    names = {s.name for s in bridge.list_tools()}
    assert META_RUN_TOOL_NAME in names
    # All 6 meta-tools present.
    assert _META_NAMES.issubset(names)


def test_skill_run_always_exposed_in_each_mode() -> None:
    """skill_run is the only path in ``unified`` and an alias in
    ``inline``; either way list_tools() must include it."""
    reg = _registry_with_n(3)
    for mode in (
        DISCLOSURE_MODE_INLINE,
        DISCLOSURE_MODE_UNIFIED,
        DISCLOSURE_MODE_AUTO,
    ):
        bridge = SkillToolProvider(reg, disclosure_mode=mode)
        names = {s.name for s in bridge.list_tools()}
        assert META_RUN_TOOL_NAME in names, f"missing skill_run in {mode}"


# ── inline vs unified surface ──────────────────────────────────────


def test_inline_mode_keeps_per_skill_tools() -> None:
    reg = _registry_with_n(3)
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    names = {s.name for s in bridge.list_tools()}
    # All 3 per-skill tools are surfaced.
    assert "skill_demo__skill_0" in names
    assert "skill_demo__skill_1" in names
    assert "skill_demo__skill_2" in names
    # Plus the 6 meta-tools.
    assert _META_NAMES.issubset(names)


def test_unified_mode_drops_per_skill_tools() -> None:
    reg = _registry_with_n(5)
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_UNIFIED)
    specs = bridge.list_tools()
    names = {s.name for s in specs}
    # Per-skill tools gone.
    for i in range(5):
        assert f"skill_demo__skill_{i}" not in names
    # But meta-tools (incl. skill_run) all still there.
    assert _META_NAMES == names


# ── auto mode threshold ────────────────────────────────────────────


def test_auto_mode_below_threshold_is_inline() -> None:
    """N=10 < threshold=20 → auto resolves to inline (per-skill tools
    visible)."""
    reg = _registry_with_n(10)
    bridge = SkillToolProvider(
        reg,
        disclosure_mode=DISCLOSURE_MODE_AUTO,
        unified_threshold=20,
    )
    names = {s.name for s in bridge.list_tools()}
    assert "skill_demo__skill_0" in names
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_INLINE


def test_auto_mode_above_threshold_is_unified() -> None:
    """N=25 > threshold=20 → auto resolves to unified (per-skill
    tools dropped, only meta-tools)."""
    reg = _registry_with_n(25)
    bridge = SkillToolProvider(
        reg,
        disclosure_mode=DISCLOSURE_MODE_AUTO,
        unified_threshold=20,
    )
    names = {s.name for s in bridge.list_tools()}
    assert "skill_demo__skill_0" not in names
    assert _META_NAMES == names
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_UNIFIED


def test_auto_mode_at_exact_threshold_is_inline() -> None:
    """Boundary: equal counts stay inline (``>`` strict, so 20 == 20
    keeps inline). Pins the boundary direction so we don't accidentally
    flip a popular config's posture."""
    reg = _registry_with_n(20)
    bridge = SkillToolProvider(
        reg,
        disclosure_mode=DISCLOSURE_MODE_AUTO,
        unified_threshold=20,
    )
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_INLINE


def test_invalid_mode_string_falls_back_to_auto() -> None:
    """Config typo doesn't crash daemon boot — falls back to auto."""
    reg = _registry_with_n(3)
    bridge = SkillToolProvider(reg, disclosure_mode="wat")
    # 3 < 20 default threshold → inline.
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_INLINE


def test_negative_threshold_clamped_to_zero() -> None:
    """A pathological negative threshold means 'always unified'."""
    reg = _registry_with_n(0)
    bridge = SkillToolProvider(
        reg, disclosure_mode=DISCLOSURE_MODE_AUTO,
        unified_threshold=-5,
    )
    # 0 > 0 == False → still inline with no skills. Add one to flip.
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_INLINE
    reg.register(_EchoSkill("x"), SkillManifest(id="x", version=1))
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_UNIFIED


def test_non_numeric_threshold_falls_back_to_default() -> None:
    reg = _registry_with_n(3)
    bridge = SkillToolProvider(
        reg, disclosure_mode=DISCLOSURE_MODE_AUTO,
        unified_threshold="not-a-number",  # type: ignore[arg-type]
    )
    # Default 20 — 3 < 20 → inline.
    assert bridge._effective_disclosure_mode() == DISCLOSURE_MODE_INLINE


# ── skill_run invocation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_run_invokes_registered_skill_with_args() -> None:
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("demo.echo"),
        SkillManifest(id="demo.echo", version=1),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "demo.echo", "args": {"hello": "world"}},
        provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == {"echoed": {"hello": "world"}}


@pytest.mark.asyncio
async def test_skill_run_omitted_args_passes_empty_dict() -> None:
    """skill_run('demo.echo') with no args → skill receives args={}."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("demo.echo"),
        SkillManifest(id="demo.echo", version=1),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "demo.echo"},
        provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == {"echoed": {}}


@pytest.mark.asyncio
async def test_skill_run_missing_skill_id_is_error() -> None:
    bridge = SkillToolProvider(SkillRegistry())
    result = await bridge.invoke(ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"args": {"x": 1}},
        provenance="synthetic",
    ))
    assert result.ok is False
    assert "skill_id" in (result.error or "")


@pytest.mark.asyncio
async def test_skill_run_non_object_args_is_error() -> None:
    """``args: "hello"`` is the kind of malformed call the LLM
    occasionally emits — surface as structured error, not crash."""
    bridge = SkillToolProvider(SkillRegistry())
    result = await bridge.invoke(ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "demo.echo", "args": "not a dict"},
        provenance="synthetic",
    ))
    assert result.ok is False
    assert "object" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_skill_run_unknown_skill_surfaces_error() -> None:
    bridge = SkillToolProvider(SkillRegistry())
    result = await bridge.invoke(ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "does.not.exist", "args": {}},
        provenance="synthetic",
    ))
    assert result.ok is False
    # The skill lookup ultimately fails with UnknownSkillError, which
    # gets coerced to ``not at HEAD`` by the shared invocation path.
    assert "not at HEAD" in (result.error or "") or \
        "unknown" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_skill_run_skill_failure_surfaces_ok_false() -> None:
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("flaky", fail=True),
        SkillManifest(id="flaky", version=1),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name=META_RUN_TOOL_NAME,
        args={"skill_id": "flaky", "args": {}},
        provenance="synthetic",
    ))
    assert result.ok is False
    assert result.error == "boom"


# ── prefilter integration ─────────────────────────────────────────


def test_prefilter_always_lets_skill_run_through() -> None:
    """B-299-style always-on guarantee: under a big library that
    triggers prefilter top-K trimming, ``skill_run`` MUST still be
    in the survivor list regardless of token overlap with the query."""
    reg = _registry_with_n(50)
    # Force unified-mode wouldn't help — prefilter only operates on
    # what list_tools() returned. Pin the inline case here because
    # that's where the prefilter has work to do.
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    full = bridge.list_tools()
    assert len(full) > 50  # 50 skills + 6 meta-tools
    # CJK query against the English-named demo skills → zero token
    # overlap, so without the always-on guarantee everything would
    # drop. Pass through min_skills_to_filter to force trimming.
    pruned = select_relevant_skills(
        "天气预报",
        full,
        top_k=12,
        min_skills_to_filter=10,
    )
    names = {s.name for s in pruned}
    assert META_RUN_TOOL_NAME in names
    # Other meta-tools also preserved.
    assert META_BROWSE_TOOL_NAME in names
    assert META_VIEW_TOOL_NAME in names
