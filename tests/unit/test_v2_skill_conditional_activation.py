"""Epic #27 G-05 (2026-05-19) — conditional skill activation via paths.

Pins:
  * Manifest ``paths`` glob list propagates onto the ToolSpec schema
    under ``x_paths`` (so the prefilter can read it).
  * Prefilter boosts a skill when active_paths match its globs.
  * Prefilter gates a skill out when it declares paths but NONE match.
  * Skills without paths frontmatter unaffected (legacy preserved).
  * ``extract_recent_paths`` harvests file-op paths from message
    history (dict + dataclass tool_call shapes; ignores bash + skill_).
  * Cross-platform path matching (Windows backslashes normalised,
    case-insensitive against globs, bare-name globs match basenames).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.prefilter import (
    _path_matches_any,
    extract_recent_paths,
    select_relevant_skills,
)
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import (
    DISCLOSURE_MODE_INLINE,
    SkillToolProvider,
)


class _NoopSkill(Skill):
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result="ok", side_effects=[])


def _registry_with(*manifests: SkillManifest) -> SkillRegistry:
    reg = SkillRegistry()
    for m in manifests:
        reg.register(_NoopSkill(m.id), m)
    return reg


# ── path matcher unit ─────────────────────────────────────────────


def test_path_match_exact_filename() -> None:
    """``package.json`` glob matches any file with that basename."""
    assert _path_matches_any("/repo/package.json", ["package.json"])
    assert _path_matches_any(
        "C:\\Users\\me\\proj\\package.json", ["package.json"],
    )


def test_path_match_glob_recursive() -> None:
    """``src/**/*.tsx`` style cross-segment glob matches nested TSX."""
    assert _path_matches_any(
        "src/components/Button.tsx", ["src/**/*.tsx"],
    )
    assert _path_matches_any(
        "src/foo/bar/baz/Form.tsx", ["src/**/*.tsx"],
    )
    assert not _path_matches_any(
        "lib/Button.tsx", ["src/**/*.tsx"],
    )


def test_path_match_case_insensitive() -> None:
    """User on Windows writes ``Src\\App.TSX`` but skill declares
    ``src/**/*.tsx`` — should still match."""
    assert _path_matches_any(
        "Src\\Components\\App.TSX", ["src/**/*.tsx"],
    )


def test_path_match_no_globs_means_no_match() -> None:
    assert not _path_matches_any("anything", [])
    assert not _path_matches_any("anything", ["", "  "])


def test_path_match_extension_glob() -> None:
    """Bare ``*.py`` glob matches any python file."""
    assert _path_matches_any("scripts/build.py", ["*.py"])
    assert _path_matches_any("/abs/path/foo.py", ["*.py"])
    assert not _path_matches_any("README.md", ["*.py"])


# ── manifest → spec schema wiring ──────────────────────────────────


def test_manifest_paths_propagate_onto_spec_schema() -> None:
    """SkillToolProvider must stamp manifest.paths under x_paths so
    the prefilter can read the gate list."""
    m = SkillManifest(
        id="react-helper",
        version=1,
        paths=("src/**/*.tsx", "package.json"),
    )
    reg = _registry_with(m)
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    specs = bridge.list_tools()
    spec = next(s for s in specs if s.name == "skill_react-helper")
    schema = spec.parameters_schema
    assert isinstance(schema, dict)
    assert schema.get("x_paths") == ["src/**/*.tsx", "package.json"]


def test_manifest_without_paths_omits_x_paths_key() -> None:
    """Skills without conditional paths should NOT get an x_paths key
    (its absence is the prefilter's signal to skip the gate)."""
    m = SkillManifest(id="general-helper", version=1)
    reg = _registry_with(m)
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    spec = next(
        s for s in bridge.list_tools()
        if s.name == "skill_general-helper"
    )
    assert "x_paths" not in (spec.parameters_schema or {})


# ── prefilter boost + gate behaviour ───────────────────────────────


def _make_30_filler_skills() -> list[SkillManifest]:
    """Push past min_skills_to_filter=30 so the prefilter actually
    runs. Each filler skill has unique token content so they don't
    collide with the test's targeted query."""
    return [
        SkillManifest(
            id=f"filler-{i}",
            version=1,
            description=f"filler skill {i} doing miscellaneous task",
        )
        for i in range(30)
    ]


def test_prefilter_boosts_skill_when_path_matches() -> None:
    """A path-conditional skill whose globs match the active path
    should rank into the top-K survivors even with weak token
    overlap on the user query."""
    react_helper = SkillManifest(
        id="react-helper",
        version=1,
        description="generic helper",  # weak token signal
        paths=("src/**/*.tsx",),
    )
    reg = _registry_with(react_helper, *_make_30_filler_skills())
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    full = bridge.list_tools()
    survivors = select_relevant_skills(
        "fix the form",  # no react/tsx tokens
        full,
        top_k=5,
        min_skills_to_filter=10,
        active_paths=["src/components/Form.tsx"],
    )
    names = [s.name for s in survivors]
    assert "skill_react-helper" in names, (
        f"path-boosted skill should have survived; got {names}"
    )


def test_prefilter_gates_out_path_skill_when_no_match() -> None:
    """A skill with ``paths`` set but no active_path match should be
    dropped from results (explicit opt-in to conditional activation
    is also an opt-out from unrelated contexts)."""
    react_helper = SkillManifest(
        id="react-helper",
        version=1,
        description="react helper",  # would score on the token, but...
        paths=("src/**/*.tsx",),
    )
    reg = _registry_with(react_helper, *_make_30_filler_skills())
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    full = bridge.list_tools()
    survivors = select_relevant_skills(
        "react helper please",
        full,
        top_k=12,
        min_skills_to_filter=10,
        # Wrong file context — agent is editing a Python file
        active_paths=["scripts/build.py", "README.md"],
    )
    names = [s.name for s in survivors]
    assert "skill_react-helper" not in names, (
        "skill_react-helper should have been gated out by path mismatch"
    )


def test_prefilter_legacy_skill_without_paths_untouched_by_gate() -> None:
    """Skills without ``paths`` frontmatter are unaffected — they
    continue ranking on token overlap regardless of active_paths."""
    legacy = SkillManifest(
        id="general-helper",
        version=1,
        description="general help with code review tasks",
    )
    reg = _registry_with(legacy, *_make_30_filler_skills())
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    full = bridge.list_tools()
    survivors = select_relevant_skills(
        "code review please",
        full,
        top_k=12,
        min_skills_to_filter=10,
        active_paths=["src/components/Form.tsx"],  # irrelevant to skill
    )
    names = [s.name for s in survivors]
    assert "skill_general-helper" in names, (
        "non-path skill should not be affected by path gate"
    )


def test_prefilter_empty_active_paths_does_not_penalise() -> None:
    """When agent hasn't touched any files yet (empty active_paths),
    path-conditional skills should still be reachable via token
    overlap. The gate only fires when active_paths is non-empty and
    the skill's globs miss all of them."""
    react_helper = SkillManifest(
        id="react-helper",
        version=1,
        description="react component helper for tsx files",
        paths=("src/**/*.tsx",),
    )
    reg = _registry_with(react_helper, *_make_30_filler_skills())
    bridge = SkillToolProvider(reg, disclosure_mode=DISCLOSURE_MODE_INLINE)
    full = bridge.list_tools()
    survivors = select_relevant_skills(
        "react component",  # strong token overlap
        full,
        top_k=12,
        min_skills_to_filter=10,
        active_paths=[],  # no file context yet
    )
    names = [s.name for s in survivors]
    assert "skill_react-helper" in names, (
        "empty active_paths should not gate a path-conditional skill"
    )


# ── extract_recent_paths from messages ─────────────────────────────


@dataclass
class _FakeToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class _FakeMessage:
    role: str
    content: str = ""
    tool_calls: list[_FakeToolCall] = field(default_factory=list)


def test_extract_recent_paths_pulls_from_file_op_calls() -> None:
    msgs = [
        _FakeMessage(role="user", content="open my file"),
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_read", args={"path": "src/App.tsx"},
            )],
        ),
        _FakeMessage(role="tool", content="..."),
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_write", args={"path": "src/App.tsx"},
            )],
        ),
    ]
    paths = extract_recent_paths(msgs, lookback=8)
    # Newest-first + de-dup → just the one path.
    assert paths == ["src/App.tsx"]


def test_extract_recent_paths_ignores_bash_and_skill() -> None:
    """Only the curated ``_FS_TOOL_NAMES`` set contributes paths.
    Bash commands and skill_<id> calls have free-form args and
    shouldn't poison the active-path window."""
    msgs = [
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="bash",
                args={"command": "cat /etc/hosts"},
            )],
        ),
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="skill_demo__foo",
                args={"path": "/should/not/leak.py"},
            )],
        ),
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="list_dir",
                args={"path": "src/"},
            )],
        ),
    ]
    paths = extract_recent_paths(msgs)
    assert paths == ["src/"]


def test_extract_recent_paths_handles_dict_messages() -> None:
    """LLM provider may emit dict-shaped messages; extract works
    against either shape."""
    msgs = [
        {
            "role": "assistant",
            "tool_calls": [
                {"name": "file_read", "args": {"path": "a.txt"}},
                {"name": "file_write", "args": {"path": "b.txt"}},
            ],
        },
    ]
    paths = extract_recent_paths(msgs)
    # Order within a single message follows args iteration order.
    assert set(paths) == {"a.txt", "b.txt"}


def test_extract_recent_paths_dedups_and_caps_results() -> None:
    msgs = []
    for i in range(50):
        msgs.append(_FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_read",
                args={"path": f"file_{i}.py"},
            )],
        ))
    # Duplicate the first path many times — should appear only once.
    msgs.extend([
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_read",
                args={"path": "file_0.py"},
            )],
        ) for _ in range(5)
    ])
    paths = extract_recent_paths(msgs, lookback=20, max_paths=10)
    assert len(paths) == 10
    # De-duped (no path appears twice).
    assert len(set(paths)) == 10


def test_extract_recent_paths_empty_input() -> None:
    assert extract_recent_paths(None) == []
    assert extract_recent_paths([]) == []


def test_extract_recent_paths_skips_messages_without_tool_calls() -> None:
    """Plain user / assistant text messages contribute zero paths."""
    msgs = [
        _FakeMessage(role="user", content="touched src/foo.py manually"),
        _FakeMessage(role="assistant", content="ok"),
        _FakeMessage(role="user", content="more talk"),
    ]
    assert extract_recent_paths(msgs) == []


def test_extract_recent_paths_lookback_caps_scan() -> None:
    """lookback=2 limits how far back we scan past tool-call messages.
    Older tool calls beyond the window should not contribute."""
    msgs = [
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_read", args={"path": "old.py"},
            )],
        ),
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_read", args={"path": "mid.py"},
            )],
        ),
        _FakeMessage(
            role="assistant",
            tool_calls=[_FakeToolCall(
                name="file_read", args={"path": "new.py"},
            )],
        ),
    ]
    paths = extract_recent_paths(msgs, lookback=2)
    # newest 2 tool-call messages scanned → new.py + mid.py
    assert "new.py" in paths
    assert "mid.py" in paths
    assert "old.py" not in paths
