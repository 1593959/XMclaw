"""recall_user_preferences tool — Epic #24 Phase 4.2.

Locks the contract:

* Tool only advertised when persona_dir is wired (no provider →
  no spec; matches existing remember/learn_about_user gating).
* Empty / missing USER.md → returns [] with friendly note.
* USER.md without ``## Auto-extracted preferences`` section → []
  with explanatory note (distinguishes from "section exists but
  empty").
* Lines matching ``ProfileDelta.render_line()`` shape get parsed
  back to {kind, text, confidence, session}.
* ``topic`` substring filter (case-insensitive on text).
* ``kind`` exact filter (case-insensitive).
* ``limit`` caps results (1-50).
* Hand-curated lines without the ``[auto · …]`` prefix are ignored
  (they're user's own writing, not extracted deltas).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _make_user_md(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _tools_with_persona(persona_dir: Path) -> BuiltinTools:
    return BuiltinTools(
        allowed_dirs=[str(persona_dir.parent)],
        persona_dir_provider=lambda: persona_dir,
    )


def _call(name: str, **args) -> ToolCall:
    return ToolCall(
        name=name, args=args, provenance="test", id="tc-1",
    )


# ── advertisement gating ────────────────────────────────────────────


def test_tool_advertised_only_with_persona_provider() -> None:
    bare = BuiltinTools()
    assert "recall_user_preferences" not in {t.name for t in bare.list_tools()}

    with_persona = BuiltinTools(persona_dir_provider=lambda: Path("/tmp/x"))
    assert "recall_user_preferences" in {t.name for t in with_persona.list_tools()}


# ── empty / missing file ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_user_md_returns_empty(tmp_path: Path) -> None:
    persona = tmp_path / "default"
    persona.mkdir()
    tools = _tools_with_persona(persona)
    result = await tools.invoke(_call("recall_user_preferences"))
    assert result.ok
    assert result.content["entries"] == []
    assert "not yet created" in result.content["note"]


@pytest.mark.asyncio
async def test_user_md_without_section_returns_empty(tmp_path: Path) -> None:
    persona = tmp_path / "default"
    _make_user_md(
        persona / "USER.md",
        "# About me\n\nI like long walks on the beach.\n",
    )
    tools = _tools_with_persona(persona)
    result = await tools.invoke(_call("recall_user_preferences"))
    assert result.ok
    assert result.content["entries"] == []
    assert "Auto-extracted" in result.content["note"]


# ── parsing ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parses_profile_delta_render_line(tmp_path: Path) -> None:
    """ProfileDelta.render_line() output round-trips through parser."""
    persona = tmp_path / "default"
    body = (
        "# About me\n\n"
        "Hand-curated stuff here.\n\n"
        "## Auto-extracted preferences\n\n"
        "- [auto · preference · conf=0.85 · session=sess-1] "
        "User prefers terse markdown answers\n"
        "- [auto · constraint · conf=0.95 · session=sess-2] "
        "Never run rm -rf without confirmation\n"
        "- [auto · habit · conf=0.70 · session=sess-3] "
        "Always rebases before pushing\n"
    )
    _make_user_md(persona / "USER.md", body)
    tools = _tools_with_persona(persona)
    result = await tools.invoke(_call("recall_user_preferences"))
    assert result.ok
    entries = result.content["entries"]
    assert len(entries) == 3
    assert entries[0] == {
        "kind": "preference",
        "text": "User prefers terse markdown answers",
        "confidence": 0.85,
        "session": "sess-1",
    }
    assert entries[2]["kind"] == "habit"
    assert entries[2]["confidence"] == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_hand_curated_lines_ignored(tmp_path: Path) -> None:
    """Lines without ``[auto · …]`` prefix don't show up — those are
    the user's own writing, not auto-extracted deltas."""
    persona = tmp_path / "default"
    body = (
        "## Auto-extracted preferences\n\n"
        "- I'm a senior dev (hand-written)\n"
        "- [auto · style · conf=0.8 · session=sx] "
        "Wants Markdown formatted output\n"
        "- And another hand-written line\n"
    )
    _make_user_md(persona / "USER.md", body)
    tools = _tools_with_persona(persona)
    result = await tools.invoke(_call("recall_user_preferences"))
    assert result.ok
    entries = result.content["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "style"


# ── filters ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topic_filter_case_insensitive(tmp_path: Path) -> None:
    persona = tmp_path / "default"
    body = (
        "## Auto-extracted preferences\n\n"
        "- [auto · preference · conf=0.85 · session=s] Prefers Markdown\n"
        "- [auto · preference · conf=0.85 · session=s] Likes git workflows\n"
        "- [auto · preference · conf=0.85 · session=s] Detests verbose prose\n"
    )
    _make_user_md(persona / "USER.md", body)
    tools = _tools_with_persona(persona)

    result = await tools.invoke(_call("recall_user_preferences", topic="MARKDOWN"))
    assert len(result.content["entries"]) == 1
    assert "Markdown" in result.content["entries"][0]["text"]


@pytest.mark.asyncio
async def test_kind_filter(tmp_path: Path) -> None:
    persona = tmp_path / "default"
    body = (
        "## Auto-extracted preferences\n\n"
        "- [auto · preference · conf=0.8 · session=s] A pref\n"
        "- [auto · constraint · conf=0.9 · session=s] A rule\n"
        "- [auto · style · conf=0.7 · session=s] A style\n"
    )
    _make_user_md(persona / "USER.md", body)
    tools = _tools_with_persona(persona)

    result = await tools.invoke(_call("recall_user_preferences", kind="constraint"))
    assert len(result.content["entries"]) == 1
    assert result.content["entries"][0]["kind"] == "constraint"


@pytest.mark.asyncio
async def test_limit_caps_results(tmp_path: Path) -> None:
    persona = tmp_path / "default"
    body = "## Auto-extracted preferences\n\n" + "\n".join(
        f"- [auto · preference · conf=0.8 · session=s{i}] Item {i}"
        for i in range(20)
    )
    _make_user_md(persona / "USER.md", body)
    tools = _tools_with_persona(persona)

    result = await tools.invoke(_call("recall_user_preferences", limit=5))
    assert len(result.content["entries"]) == 5


# ── stops at next top-level heading ─────────────────────────────────


@pytest.mark.asyncio
async def test_section_terminates_at_next_h2(tmp_path: Path) -> None:
    """Don't bleed into next ## section — that's user content, not deltas."""
    persona = tmp_path / "default"
    body = (
        "## Auto-extracted preferences\n\n"
        "- [auto · preference · conf=0.8 · session=s] Real delta\n"
        "\n## My own notes\n\n"
        "- [auto · fake · conf=0.99 · session=spoof] Spoofed line\n"
    )
    _make_user_md(persona / "USER.md", body)
    tools = _tools_with_persona(persona)

    result = await tools.invoke(_call("recall_user_preferences"))
    entries = result.content["entries"]
    assert len(entries) == 1
    assert entries[0]["text"] == "Real delta"


# ── bad inputs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_limit_returns_error(tmp_path: Path) -> None:
    persona = tmp_path / "default"
    persona.mkdir()
    tools = _tools_with_persona(persona)
    result = await tools.invoke(
        _call("recall_user_preferences", limit="not-a-number"),
    )
    assert not result.ok
    assert "integer" in result.error.lower()
