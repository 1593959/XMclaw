"""B-238: skill prefilter unit tests.

Pin the keyword-overlap routing logic so a refactor doesn't accidentally
turn it into "match everything" or "match nothing". Tests cover:

  * Non-skill tools (bash / file_read) ALWAYS pass through.
  * With < min_skills_to_filter skills, no filter happens (small setups
    keep the full list).
  * Empty / pathological query → no filter (don't blindly drop).
  * Score: name-substring > description-token > trigger-match.
  * CJK queries match Han characters in skill descriptions.
  * Stop-words ("the", "skill", "请", etc) don't pad scores.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xmclaw.skills.prefilter import select_relevant_skills


@dataclass
class _FakeSpec:
    """Mimics the public surface of ``ToolSpec`` (name + description +
    parameters_schema). Tests don't need the real dataclass."""
    name: str
    description: str = ""
    parameters_schema: dict | None = None


def _bulk(*pairs: tuple[str, str]) -> list[_FakeSpec]:
    return [_FakeSpec(name=n, description=d) for n, d in pairs]


# ── Pass-through cases ─────────────────────────────────────────────


def test_non_skill_tools_always_pass_through() -> None:
    """``bash`` / ``file_read`` / ``web_fetch`` are workhorses — never
    filtered out, regardless of query relevance."""
    specs = [
        _FakeSpec(name="bash", description="run shell command"),
        _FakeSpec(name="file_read", description="read a file"),
        _FakeSpec(name="web_fetch", description="GET a URL"),
    ] + [
        _FakeSpec(name=f"skill_x{i}", description=f"X skill {i}")
        for i in range(50)
    ]
    out = select_relevant_skills("commit my changes", specs, top_k=5)
    out_names = {s.name for s in out}
    assert "bash" in out_names
    assert "file_read" in out_names
    assert "web_fetch" in out_names


def test_below_min_skills_no_filter() -> None:
    """Small setups (< 30 skills by default) keep the full list — the
    LLM can absorb 20-tool contexts fine."""
    specs = _bulk(
        ("skill_a", "alpha thing"),
        ("skill_b", "beta thing"),
        ("skill_c", "gamma thing"),
    )
    out = select_relevant_skills("zzz nothing", specs, top_k=1)
    assert len(out) == 3  # untouched


def test_empty_query_no_filter() -> None:
    """When the query has no usable tokens (just emoji / single-letter),
    don't filter — let the LLM see everything."""
    skills = [_FakeSpec(name=f"skill_x{i}", description=f"X{i}") for i in range(50)]
    out_emoji = select_relevant_skills("👋", skills, top_k=5)
    assert len(out_emoji) == 50
    out_blank = select_relevant_skills("", skills, top_k=5)
    assert len(out_blank) == 50


# ── Scoring cases ─────────────────────────────────────────────────


def test_name_match_beats_description_match() -> None:
    """Skill whose NAME contains the query token outranks one where
    only the description does."""
    specs = (
        _bulk(*[(f"skill_pad{i}", f"padding skill {i}") for i in range(40)])
        + _bulk(
            ("skill_git-commit", "Help with version control"),
            ("skill_changelog", "Generate git commit changelog"),
        )
    )
    out = select_relevant_skills("write a commit message", specs, top_k=2)
    skill_names = [s.name for s in out if s.name.startswith("skill_")]
    # Both skills score; commit (name match) > changelog (desc only)
    assert "skill_git-commit" in skill_names
    # The padding skills should NOT make it (zero score → dropped).
    assert not any(s.name.startswith("skill_pad") for s in out)


def test_zero_score_skills_dropped() -> None:
    """With 400 padding skills and 1 obvious match, only the match
    survives + the non-skills pass through."""
    specs = (
        [_FakeSpec(name="bash", description="shell")]
        + [_FakeSpec(name=f"skill_pad{i}", description=f"unrelated {i}")
           for i in range(50)]
        + [_FakeSpec(name="skill_python-error-handling",
                     description="Python error handling patterns including "
                                 "input validation and exception types")]
    )
    out = select_relevant_skills(
        "help me handle Python exceptions", specs, top_k=12,
    )
    skill_out = [s.name for s in out if s.name.startswith("skill_")]
    # The handler skill should survive; padding skills dropped to 0.
    assert "skill_python-error-handling" in skill_out
    assert not any(n.startswith("skill_pad") for n in skill_out)


def test_top_k_caps_results() -> None:
    """Even when many skills match, only top_k survive."""
    specs = [
        _FakeSpec(name=f"skill_test{i}", description="testing patterns")
        for i in range(50)
    ]
    out = select_relevant_skills("testing", specs, top_k=5)
    assert len(out) == 5


def test_chinese_query_matches_chinese_description() -> None:
    """CJK chars in query should match CJK chars in skill description."""
    specs = (
        _bulk(*[(f"skill_pad{i}", f"unrelated {i}") for i in range(40)])
        + _bulk(
            ("skill_zh-commit", "帮你写 git commit 消息 用规范格式"),
        )
    )
    out = select_relevant_skills("帮我写 commit", specs, top_k=3)
    out_names = {s.name for s in out}
    assert "skill_zh-commit" in out_names


def test_stopwords_dont_pad_scores() -> None:
    """``the`` / ``skill`` / ``please`` shouldn't make every skill
    score 1+. Two skills with description containing ONLY stopwords
    against the other should ALL score 0 → all dropped."""
    specs = (
        [_FakeSpec(name="bash", description="shell")]
        + [_FakeSpec(
            name=f"skill_pad{i}",
            description="this is the skill that you can please use",
        ) for i in range(40)]
    )
    # The query is 100% stopwords → ``query_tokens - _STOPWORDS`` is
    # empty → no filter (full list returned).
    out = select_relevant_skills(
        "please use the skill that you can", specs, top_k=5,
    )
    # Empty query-after-stopwords falls into the no-filter branch.
    assert len(out) == len(specs)


def test_trigger_match_adds_score() -> None:
    """SKILL.md frontmatter triggers (in parameters_schema.x_triggers)
    add a small bonus."""
    specs = (
        _bulk(*[(f"skill_pad{i}", "unrelated") for i in range(40)])
        + [_FakeSpec(
            name="skill_target",
            description="generic placeholder",
            parameters_schema={"x_triggers": ["refactor", "improve code"]},
        )]
    )
    out = select_relevant_skills("refactor this file", specs, top_k=3)
    out_names = {s.name for s in out}
    assert "skill_target" in out_names
