"""B-124 — SkillToolProvider unit tests.

Pins:
  * registered Skill HEAD entries surface as tools named
    ``skill_<id>`` (with ``.`` → ``__`` namespace mapping)
  * tool invocation routes back to ``skill.run(SkillInput(args=...))``
  * ok=False from a skill becomes ToolResult(ok=False, error=<msg>)
  * skill exceptions become ToolResult(ok=False, error=<repr>) — never
    propagate out of invoke()
  * skill list is dynamic — promote/rollback after construction is
    reflected on the next list_tools() / invoke() call
  * CompositeToolProvider routes correctly through the bridge, including
    the new dynamic-discovery fallback on a registered-after-wrap skill
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.composite import CompositeToolProvider
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import (
    META_BROWSE_TOOL_NAME,
    META_COMPOSE_TOOL_NAME,
    META_DECISION_TOOL_NAME,
    META_DIFF_TOOL_NAME,
    META_INSTALL_TOOL_NAME,
    META_PROPOSE_TOOL_NAME,
    META_ROLLBACK_TOOL_NAME,
    META_RUN_TOOL_NAME,
    META_STATUS_TOOL_NAME,
    META_UNINSTALL_TOOL_NAME,
    META_VIEW_TOOL_NAME,
    SkillToolProvider,
    _to_tool_name,
)


_META_TOOL_NAMES = frozenset({
    META_BROWSE_TOOL_NAME,
    META_INSTALL_TOOL_NAME,
    META_UNINSTALL_TOOL_NAME,
    # Epic #27 P0 G-01 (2026-05-19) — introspection meta-tools.
    META_STATUS_TOOL_NAME,
    META_DECISION_TOOL_NAME,
    META_VIEW_TOOL_NAME,
    # Epic #27 G-04 (2026-05-19) — progressive-disclosure run dispatcher.
    META_RUN_TOOL_NAME,
    # Epic #27 G-07 (2026-05-19) — versioned-edit history.
    META_DIFF_TOOL_NAME,
    META_ROLLBACK_TOOL_NAME,
    # Epic #27 G-08 (2026-05-19) — self-evolving skills.
    META_PROPOSE_TOOL_NAME,
    # skill_compose — sequential-workflow dispatcher (always-on meta-tool).
    META_COMPOSE_TOOL_NAME,
})


def _registered_skill_specs(bridge: SkillToolProvider) -> list:
    """Return only the registry-backed skill specs.

    B-299 added the synthesised ``skill_browse`` meta-tool;
    Wave-27 fix-LAT7 added ``skill_install`` + ``skill_uninstall``.
    All three are always-on at the head of ``list_tools()``. Tests
    that pre-date the meta-tools care about *registered* skills, so
    we filter all three out here.
    """
    return [s for s in bridge.list_tools() if s.name not in _META_TOOL_NAMES]


# ── fixtures ────────────────────────────────────────────────────────


class _EchoSkill(Skill):
    """Skill that returns whatever args it received — easy to assert."""

    def __init__(self, skill_id: str, version: int = 1, *,
                 fail: bool = False) -> None:
        self.id = skill_id
        self.version = version
        self._fail = fail

    async def run(self, inp: SkillInput) -> SkillOutput:
        if self._fail:
            return SkillOutput(
                ok=False,
                result={"error": "intentional failure", "args": inp.args},
                side_effects=[],
            )
        return SkillOutput(
            ok=True,
            result={"echoed": inp.args, "version": self.version},
            side_effects=["/tmp/test"],
        )


class _BoomSkill(Skill):
    id = "demo.boom"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        raise RuntimeError("kaboom")


def _manifest(id_: str, v: int) -> SkillManifest:
    return SkillManifest(id=id_, version=v)


def _registry_with(*skills_and_manifests) -> SkillRegistry:
    reg = SkillRegistry()
    for skill, manifest in skills_and_manifests:
        reg.register(skill, manifest, set_head=True)
    return reg


# ── name encoding ───────────────────────────────────────────────────


def test_tool_name_replaces_dots() -> None:
    assert _to_tool_name("demo.read_and_summarize") == \
        "skill_demo__read_and_summarize"


def test_tool_name_strips_invalid_chars() -> None:
    # `+` and `@` are not in [a-zA-Z0-9_-]; they get squashed to `_`.
    assert _to_tool_name("foo+bar@baz") == "skill_foo_bar_baz"


# ── list_tools ──────────────────────────────────────────────────────


def test_list_tools_exposes_registered_skills() -> None:
    reg = _registry_with(
        (_EchoSkill("demo.echo"), _manifest("demo.echo", 1)),
        (_EchoSkill("simple", 2), _manifest("simple", 2)),
    )
    bridge = SkillToolProvider(reg)
    # B-299: meta-tool is always exposed; filter for the registry-backed
    # specs we care about in this test.
    names = sorted(s.name for s in _registered_skill_specs(bridge))
    assert names == ["skill_demo__echo", "skill_simple"]


def test_list_tools_description_carries_provenance() -> None:
    """B-177 trailer format: ``[skill:<id> v<n>, [trust=<lvl>, ]by=<created_by>]``
    with evidence on a separate line below. Trust tag added by G-06
    (2026-05-19) — included unconditionally for registered skills."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("x"),
        SkillManifest(id="x", version=1, created_by="evolved",
                      evidence=("bench:1.12x",)),
    )
    bridge = SkillToolProvider(reg)
    spec = _registered_skill_specs(bridge)[0]
    # New compact trailer with trust included (G-06).
    assert "[skill:x v1" in spec.description
    assert "by=evolved]" in spec.description
    # Evidence preserved (audit trail).
    assert "bench:1.12x" in spec.description


def test_list_tools_is_dynamic_after_promotion() -> None:
    """A skill registered AFTER the bridge construction must show up
    on the next list_tools() call — no restart required."""
    reg = SkillRegistry()
    bridge = SkillToolProvider(reg)
    # B-299: meta-tool is always present; use the helper to ignore it.
    assert _registered_skill_specs(bridge) == []

    reg.register(_EchoSkill("late"), _manifest("late", 1))
    names = [s.name for s in _registered_skill_specs(bridge)]
    assert names == ["skill_late"]


# ── invoke ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_runs_skill_and_returns_result() -> None:
    reg = _registry_with(
        (_EchoSkill("demo.echo"), _manifest("demo.echo", 1)),
    )
    bridge = SkillToolProvider(reg)
    call = ToolCall(
        name="skill_demo__echo",
        args={"hello": "world"},
        provenance="synthetic",
    )
    result = await bridge.invoke(call)
    assert result.ok is True
    assert result.content == {"echoed": {"hello": "world"}, "version": 1}
    assert result.side_effects == ("/tmp/test",)
    assert result.call_id == call.id


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_structured_error() -> None:
    bridge = SkillToolProvider(SkillRegistry())
    result = await bridge.invoke(ToolCall(
        name="skill_does_not_exist",
        args={}, provenance="synthetic",
    ))
    assert result.ok is False
    assert "unknown skill tool" in (result.error or "")


@pytest.mark.asyncio
async def test_invoke_skill_returning_ok_false_surfaces_error() -> None:
    reg = _registry_with(
        (_EchoSkill("flaky", fail=True), _manifest("flaky", 1)),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name="skill_flaky", args={"x": 1}, provenance="synthetic",
    ))
    assert result.ok is False
    assert result.error == "intentional failure"


@pytest.mark.asyncio
async def test_invoke_skill_exception_does_not_propagate() -> None:
    """A skill that raises must NOT crash the agent loop — it surfaces
    as ok=False with a diagnostic error string."""
    reg = _registry_with(
        (_BoomSkill(), _manifest("demo.boom", 1)),
    )
    bridge = SkillToolProvider(reg)
    result = await bridge.invoke(ToolCall(
        name="skill_demo__boom", args={}, provenance="synthetic",
    ))
    assert result.ok is False
    assert "RuntimeError" in (result.error or "")
    assert "kaboom" in (result.error or "")


# ── composite + dynamic discovery ──────────────────────────────────


class _StubBuiltin:
    """Minimal stub that gives CompositeToolProvider one stable child."""

    def list_tools(self) -> list:
        from xmclaw.core.ir import ToolSpec
        return [ToolSpec(
            name="builtin_noop", description="noop",
            parameters_schema={"type": "object"},
        )]

    async def invoke(self, call: ToolCall):
        from xmclaw.core.ir import ToolResult
        return ToolResult(
            call_id=call.id, ok=True, content="builtin",
        )


@pytest.mark.asyncio
async def test_composite_routes_to_skill_tool_provider() -> None:
    reg = _registry_with(
        (_EchoSkill("a"), _manifest("a", 1)),
    )
    composite = CompositeToolProvider(_StubBuiltin(), SkillToolProvider(reg))
    names = {s.name for s in composite.list_tools()}
    assert "builtin_noop" in names
    assert "skill_a" in names

    # Invoke the skill via the composite — proves end-to-end routing.
    result = await composite.invoke(ToolCall(
        name="skill_a", args={"k": "v"}, provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == {"echoed": {"k": "v"}, "version": 1}


@pytest.mark.asyncio
async def test_composite_dynamic_discovery_for_late_registered_skill() -> None:
    """B-124 router fallback: a skill registered AFTER CompositeToolProvider
    construction must still be invokable. With the fallback removed,
    callers must explicitly invalidate_router() after mutations."""
    reg = SkillRegistry()
    composite = CompositeToolProvider(SkillToolProvider(reg))

    # Register AFTER the composite is built, then invalidate the router.
    reg.register(_EchoSkill("late"), _manifest("late", 1))
    composite.invalidate_router()

    # list_tools sees it (no caching there).
    assert any(
        s.name == "skill_late" for s in composite.list_tools()
    )
    # invoke must work because the router was rebuilt.
    result = await composite.invoke(ToolCall(
        name="skill_late", args={"x": 9}, provenance="synthetic",
    ))
    assert result.ok is True
    assert result.content == {"echoed": {"x": 9}, "version": 1}


# ── B-176 → B-177: tool description format ───────────────────────


def test_list_tools_description_leads_with_body() -> None:
    """B-177: description leads with the FUNCTIONAL body (verb-noun
    sentence) — same shape ``bash`` / ``file_read`` use. Pre-B-177
    the description opened with "Skill: <id> v<n> (created_by=...)"
    which framed every skill_* as second-class to the LLM."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("git-commit"),
        SkillManifest(
            id="git-commit", version=1, created_by="user",
            title="Git Commit Workflow",
            description=(
                "Execute git commit with conventional commit message "
                "analysis. Use when the user asks to commit changes."
            ),
            triggers=("/commit", "git commit", "提交"),
        ),
    )
    bridge = SkillToolProvider(reg)
    specs = _registered_skill_specs(bridge)
    assert len(specs) == 1
    desc = specs[0].description
    # Body description leads (no "Skill:" prefix at byte 0).
    assert desc.startswith("Execute git commit")
    # Triggers shown so the LLM can keyword-match.
    assert "Use when:" in desc
    assert "/commit" in desc and "提交" in desc
    # Provenance trailer kept compact at the end. G-06 (2026-05-19)
    # inserts a ``trust=...`` field between id and ``by=...``.
    assert "[skill:git-commit v1" in desc
    assert "by=user]" in desc


def test_list_tools_description_falls_back_to_title_when_no_body() -> None:
    """No description but title set → title becomes the lead line so
    the LLM still gets *something* functional rather than just the id."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("titled-only"),
        SkillManifest(
            id="titled-only", version=1, created_by="user",
            title="Generate code from spec",
        ),
    )
    bridge = SkillToolProvider(reg)
    specs = _registered_skill_specs(bridge)
    desc = specs[0].description
    assert desc.startswith("Generate code from spec")


def test_list_tools_description_minimal_when_no_frontmatter() -> None:
    """A skill with empty manifest description / title still gets a
    valid (terse) tool spec — just the id + version + provenance
    trailer. Floor behaviour."""
    reg = SkillRegistry()
    reg.register(
        _EchoSkill("bare"),
        SkillManifest(id="bare", version=1, created_by="evolved"),
    )
    bridge = SkillToolProvider(reg)
    specs = _registered_skill_specs(bridge)
    assert len(specs) == 1
    desc = specs[0].description
    # Trailer carries id + provenance. G-06 (2026-05-19) inserts
    # ``trust=...`` between id and ``by=...``.
    assert "[skill:bare v1" in desc
    assert "by=evolved]" in desc


# ── Epic #27 P0 G-01 (2026-05-19): skill_status / skill_view ─────


class _FakeWatcher:
    """Stand-in for SkillsWatcher carrying just the surface
    SkillToolProvider needs: ``load_failures()`` + ``pending_restarts()``.
    Tests inject custom rows."""

    def __init__(
        self,
        failures: list | None = None,
        restarts: list | None = None,
    ) -> None:
        self._failures = list(failures or [])
        self._restarts = list(restarts or [])

    def load_failures(self) -> list:
        return list(self._failures)

    def pending_restarts(self) -> list:
        return list(self._restarts)


def test_g01_skill_status_tool_exposed_in_list_tools() -> None:
    """skill_status + skill_view are always-on meta-tools, just like
    skill_browse / skill_install. Available even with zero skills."""
    bridge = SkillToolProvider(SkillRegistry())
    names = {s.name for s in bridge.list_tools()}
    assert META_STATUS_TOOL_NAME in names
    assert META_VIEW_TOOL_NAME in names


@pytest.mark.asyncio
async def test_g01_skill_status_no_watcher_reports_clean_state() -> None:
    """No watcher wired (test harness) → 0 failures, 0 restarts,
    healthy note. Doesn't raise."""
    reg = SkillRegistry()
    reg.register(_EchoSkill("alpha"), _manifest("alpha", 1))
    bridge = SkillToolProvider(reg, watcher=None)
    call = ToolCall(name=META_STATUS_TOOL_NAME, args={}, provenance="test")
    result = await bridge.invoke(call)
    assert result.ok
    content = result.content
    assert content["totals"]["registered_skill_ids"] == 1
    assert content["load_failures"] == []
    assert content["pending_restarts"] == []
    assert "clean state" in " ".join(content["notes"]).lower()


@pytest.mark.asyncio
async def test_skill_decision_records_structured_skip_reason() -> None:
    bridge = SkillToolProvider(SkillRegistry())
    call = ToolCall(
        name=META_DECISION_TOOL_NAME,
        args={
            "action": "skip",
            "skill_id": "frontend.ui-review",
            "skip_reason": "candidate_not_applicable_to_task",
            "note": "task is backend only",
        },
        provenance="test",
    )

    result = await bridge.invoke(call)

    assert result.ok
    assert result.content["kind"] == "skill_decision"
    assert result.content["action"] == "skip"
    assert result.content["skip_reason"] == "candidate_not_applicable_to_task"
    assert result.metadata["kind"] == "skill_decision"


@pytest.mark.asyncio
async def test_skill_decision_requires_reason_for_skip() -> None:
    bridge = SkillToolProvider(SkillRegistry())
    call = ToolCall(
        name=META_DECISION_TOOL_NAME,
        args={"action": "skip", "skill_id": "frontend.ui-review"},
        provenance="test",
    )

    result = await bridge.invoke(call)

    assert not result.ok
    assert "requires skip_reason" in (result.error or "")


@pytest.mark.asyncio
async def test_g01_skill_status_surfaces_load_failures_and_restarts(
) -> None:
    """Watcher reports a broken skill + a fixed-after-failure restart.
    The status output gives the agent enough info to act:
      - Read .path + .error of the broken skill
      - Tell the user to restart the daemon for the fix to load"""
    reg = SkillRegistry()
    watcher = _FakeWatcher(
        failures=[{
            "skill_id": "hyper-broken",
            "path": "/tmp/skills/hyper-broken/skill.py",
            "kind": "python",
            "error": "no concrete Skill subclass found",
            "ticks_failing": 8,
            "first_seen": 1000.0,
            "last_seen": 1080.0,
        }],
        restarts=[{
            "skill_id": "hyper-fixed",
            "version": 1,
            "path": "/tmp/skills/hyper-fixed/skill.py",
            "state": "fixed_after_failure",
            "registered": False,
        }],
    )
    bridge = SkillToolProvider(reg, watcher=watcher)
    call = ToolCall(name=META_STATUS_TOOL_NAME, args={}, provenance="test")
    result = await bridge.invoke(call)
    assert result.ok
    content = result.content
    assert len(content["load_failures"]) == 1
    assert content["load_failures"][0]["skill_id"] == "hyper-broken"
    assert content["load_failures"][0]["ticks_failing"] == 8
    assert len(content["pending_restarts"]) == 1
    assert content["pending_restarts"][0]["state"] == "fixed_after_failure"
    # Notes call out both situations so the agent reads + acts.
    notes_text = " ".join(content["notes"])
    assert "failed to load" in notes_text
    assert "restart" in notes_text.lower()


@pytest.mark.asyncio
async def test_g01_skill_view_reads_markdown_body(tmp_path: Path) -> None:
    """skill_view returns the SKILL.md body + file inventory so the
    agent can inspect a skill's instructions before invoking."""
    # Build a fake skill dir under a tmp skills_user root.
    skills_root = tmp_path / "skills_user"
    skill_dir = skills_root / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: a demo\n---\n# Demo\n\nSteps:\n1. foo\n",
        encoding="utf-8",
    )
    (skill_dir / "manifest.json").write_text(
        '{"id": "demo-skill", "version": 1}', encoding="utf-8",
    )
    # Point resolve_skill_roots at our tmp via monkeypatch on user_skills_dir.
    import xmclaw.skills.user_loader as ul
    import xmclaw.utils.paths as paths

    original = paths.user_skills_dir
    paths.user_skills_dir = lambda: skills_root  # type: ignore[assignment]
    ul.user_skills_dir = paths.user_skills_dir  # keep both in sync
    try:
        bridge = SkillToolProvider(SkillRegistry())
        call = ToolCall(
            name=META_VIEW_TOOL_NAME,
            args={"skill_id": "demo-skill"},
            provenance="test",
        )
        result = await bridge.invoke(call)
        assert result.ok, result.error
        c = result.content
        assert c["kind"] == "markdown"
        assert "# Demo" in c["body"]
        assert any(f["name"] == "manifest.json" for f in c["files"])
    finally:
        paths.user_skills_dir = original
        ul.user_skills_dir = original


@pytest.mark.asyncio
async def test_g01_skill_view_rejects_path_traversal(tmp_path: Path) -> None:
    """file_path with ``..`` or absolute paths is refused — the
    agent shouldn't be able to ``skill_view(skill_id='x',
    file_path='../../etc/passwd')`` itself out of the skill dir."""
    skills_root = tmp_path / "skills_user"
    (skills_root / "demo").mkdir(parents=True)
    (skills_root / "demo" / "SKILL.md").write_text("# d", encoding="utf-8")

    import xmclaw.skills.user_loader as ul
    import xmclaw.utils.paths as paths

    original = paths.user_skills_dir
    paths.user_skills_dir = lambda: skills_root  # type: ignore[assignment]
    ul.user_skills_dir = paths.user_skills_dir
    try:
        bridge = SkillToolProvider(SkillRegistry())
        for bad in ("../../etc/passwd", "/etc/passwd"):
            call = ToolCall(
                name=META_VIEW_TOOL_NAME,
                args={"skill_id": "demo", "file_path": bad},
                provenance="test",
            )
            result = await bridge.invoke(call)
            assert not result.ok, f"{bad} should have been rejected"
            assert "relative" in (result.error or "").lower() or \
                ".." in (result.error or "")
    finally:
        paths.user_skills_dir = original
        ul.user_skills_dir = original


@pytest.mark.asyncio
async def test_g01_skill_view_missing_skill_reports_error(tmp_path: Path) -> None:
    """skill_view on an id that doesn't exist on disk returns a
    clear error rather than 500ing or hanging."""
    skills_root = tmp_path / "skills_user"
    skills_root.mkdir()
    import xmclaw.skills.user_loader as ul
    import xmclaw.utils.paths as paths

    original = paths.user_skills_dir
    paths.user_skills_dir = lambda: skills_root  # type: ignore[assignment]
    ul.user_skills_dir = paths.user_skills_dir
    try:
        bridge = SkillToolProvider(SkillRegistry())
        call = ToolCall(
            name=META_VIEW_TOOL_NAME,
            args={"skill_id": "does-not-exist"},
            provenance="test",
        )
        result = await bridge.invoke(call)
        assert not result.ok
        assert "not found" in (result.error or "").lower()
    finally:
        paths.user_skills_dir = original
        ul.user_skills_dir = original
