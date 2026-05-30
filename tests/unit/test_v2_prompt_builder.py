"""Prompt builder — section assembly and utility functions.

Covers Phase 4's section-based architecture: PromptSection dataclass,
_assemble_sections, _get_static_system_prompt, _build_time_block.
"""
from __future__ import annotations

from xmclaw.daemon.prompt_builder import (
    PromptSection,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    _assemble_sections,
    _build_time_block,
    _get_static_system_prompt,
    _with_fresh_time,
)


def test_assemble_sections_joins_with_stamps() -> None:
    """Each section gets an HTML-comment stamp; parts are separated
    by double-newlines."""
    out = _assemble_sections([
        PromptSection("identity", "1.0.0", "You are X."),
        PromptSection("rules", "2.1.0", "Be nice."),
    ])
    assert "<!-- section:identity version:1.0.0 -->" in out
    assert "<!-- section:rules version:2.1.0 -->" in out
    assert "You are X." in out
    assert "Be nice." in out
    # Double-newline separator between sections.
    assert "You are X.\n\n<!-- section:rules" in out


def test_assemble_sections_rstrips_trailing_whitespace() -> None:
    """Sections ending with newlines / spaces get cleaned so the
    final output doesn't carry indent artifacts from triple-quoted
    string literals."""
    out = _assemble_sections([
        PromptSection("a", "1.0.0", "Hello\n    "),
    ])
    assert out == "<!-- section:a version:1.0.0 -->\n\nHello"


def test_assemble_sections_empty_list_returns_empty() -> None:
    assert _assemble_sections([]) == ""


def test_get_static_system_prompt_strips_boundary() -> None:
    """Everything after SYSTEM_PROMPT_DYNAMIC_BOUNDARY is removed."""
    raw = "Static prefix\n\n" + SYSTEM_PROMPT_DYNAMIC_BOUNDARY + "\n\nDynamic tail"
    static = _get_static_system_prompt(raw)
    assert "Static prefix" in static
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in static
    assert "Dynamic tail" not in static


def test_get_static_system_prompt_no_boundary_returns_unchanged() -> None:
    raw = "Just a plain prompt"
    assert _get_static_system_prompt(raw) == raw


def test_get_static_system_prompt_strips_legacy_time_block() -> None:
    """Legacy prompts that embed '## 当前时刻' inside the static
    portion have that block removed."""
    raw = "Prefix\n## 当前时刻\n2024-01-01\n## Other"
    static = _get_static_system_prompt(raw)
    assert "当前时刻" not in static
    assert "Prefix" in static
    assert "## Other" in static


def test_build_time_block_contains_timestamp() -> None:
    tb = _build_time_block()
    assert tb.startswith("## 当前时刻")
    # Should contain today's year.
    from datetime import datetime
    year = str(datetime.now().year)
    assert year in tb


def test_with_fresh_time_appends_boundary_and_time() -> None:
    base = "Static"
    full = _with_fresh_time(base)
    assert full.startswith("Static")
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in full
    assert "## 当前时刻" in full


def test_prompt_section_immutable() -> None:
    """PromptSection is a dataclass but field mutation is not
    expected — the dataclass itself doesn't enforce frozen=True,
    but the public API treats it as value-like."""
    ps = PromptSection("id", "1.0.0", "content")
    assert ps.name == "id"
    assert ps.version == "1.0.0"
    assert ps.content == "content"
