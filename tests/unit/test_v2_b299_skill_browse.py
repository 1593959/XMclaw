"""B-299: pin the ``skill_browse`` meta-discovery tool.

Real-data 2026-05-07: 404 skills installed locally, but the agent
loop's B-238 prefilter narrows the LLM's tool list to top-12 by
token-overlap. CJK queries against English-described skills score
zero across the board → the LLM sees ZERO skill_* tools that turn,
and falls back to bash / web_search / file_*. The skill exists but
is invisible. ``_arms`` stays empty in EvolutionAgent → no
GRADER_VERDICT for skills → no proposals.

``skill_browse`` is the LLM's escape hatch:

* always present in ``SkillToolProvider.list_tools()``
  (synthesized, not registry-backed) — these tests pin that;
* prefilter special-cases the name to pass through even when
  every other skill scored 0 — these tests pin that;
* takes ``query: str`` + optional ``top_k`` → returns top
  matches with descriptions so the LLM can pick on its next
  turn — these tests pin the IO contract;
* gracefully handles edge cases (empty query, all-zero
  scores, CJK-only query, bad ``top_k``) — these tests pin
  the failure modes.

Together with the existing B-238 prefilter tests, these lock down
"agent has visibility into the skill catalog regardless of query
language or specificity".
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall, ToolSpec
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest
from xmclaw.skills.prefilter import select_relevant_skills
from xmclaw.skills.registry import SkillRegistry
from xmclaw.skills.tool_bridge import (
    META_BROWSE_TOOL_NAME, SkillToolProvider,
)


# ── fixtures ────────────────────────────────────────────────────────


class _NoopSkill(Skill):
    """Trivial skill — its run() body doesn't matter, the test only
    cares that it shows up in ``list_tools`` for browsing."""

    def __init__(self, sid: str) -> None:
        self.id = sid
        self.version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(ok=True, result="ok", side_effects=[])


def _registry_with_skills(*specs: tuple[str, str]) -> SkillRegistry:
    """Build a registry with ``(skill_id, description)`` pairs."""
    reg = SkillRegistry()
    for sid, desc in specs:
        reg.register(
            _NoopSkill(sid),
            SkillManifest(
                id=sid, version=1, created_by="user",
                description=desc,
            ),
        )
    return reg


# ── list_tools: meta-tool always present ───────────────────────────


def test_browse_tool_always_present_with_empty_registry() -> None:
    """Even when no user skills are registered, the LLM still sees
    skill_browse so it knows the discovery affordance exists."""
    bridge = SkillToolProvider(SkillRegistry())
    names = [s.name for s in bridge.list_tools()]
    assert META_BROWSE_TOOL_NAME in names


def test_browse_tool_first_in_list() -> None:
    """Convention: the meta-tool is at index 0. Helps stable
    iteration in tests + makes it obvious in any debug dump."""
    reg = _registry_with_skills(
        ("alpha", "First skill"),
        ("beta", "Second skill"),
    )
    bridge = SkillToolProvider(reg)
    specs = bridge.list_tools()
    assert specs[0].name == META_BROWSE_TOOL_NAME


def test_browse_tool_description_mentions_skill_count() -> None:
    """The description doubles as a 'how many are there' signal so
    the LLM sees 404 vs 4 in the tool spec itself, not just the
    registry."""
    reg = _registry_with_skills(
        ("a", ""), ("b", ""), ("c", ""),
    )
    bridge = SkillToolProvider(reg)
    spec = bridge.list_tools()[0]
    assert "3 skill" in spec.description


def test_browse_tool_schema_requires_query() -> None:
    """The schema must mark ``query`` as required so a misformed
    LLM call gets schema-validated at the call site (not at our
    runtime check)."""
    bridge = SkillToolProvider(SkillRegistry())
    spec = bridge.list_tools()[0]
    schema = spec.parameters_schema
    assert schema.get("required") == ["query"]
    assert "query" in schema.get("properties", {})
    assert "top_k" in schema.get("properties", {})


# ── invoke: scoring + IO ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_browse_returns_top_matches_for_keyword_query() -> None:
    """English query → token-overlap finds the matching skill."""
    reg = _registry_with_skills(
        ("git-commit", "Run git commit with conventional message."),
        ("deploy-vercel", "Deploy a project to Vercel."),
        ("read-file", "Read a file from disk."),
    )
    bridge = SkillToolProvider(reg)
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "git commit message"},
        provenance="test",
    ))
    assert res.ok is True
    matches = res.content["matches"]
    assert matches, "expected at least one match for the keyword query"
    # The git-commit skill should rank first.
    assert matches[0]["tool_name"] == "skill_git-commit"
    # Each match carries the trio the LLM needs.
    for m in matches:
        assert "tool_name" in m
        assert "score" in m
        assert "description" in m


@pytest.mark.asyncio
async def test_browse_returns_zero_score_skills_via_substring() -> None:
    """When the tokenizer finds no usable tokens (single CJK
    stopword, etc.), the substring fallback lets the user still
    discover skills by literal id/desc match."""
    reg = _registry_with_skills(
        # The query "天气" won't tokenize against English text, so
        # we exercise the substring fallback by putting "天气" in
        # the description so the substring scan picks it up.
        ("weather", "Check the weather (天气) for a city."),
        ("git-status", "Show git status."),
    )
    bridge = SkillToolProvider(reg)
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "天气"},
        provenance="test",
    ))
    assert res.ok is True
    names = [m["tool_name"] for m in res.content["matches"]]
    assert "skill_weather" in names


@pytest.mark.asyncio
async def test_browse_empty_registry_returns_empty_with_note() -> None:
    """Empty registry → empty matches + a 'no results' note so the
    LLM can fall back without re-asking. Design choice: when there
    ARE skills in the registry but none score > 0, browse returns
    them anyway (sorted) so the LLM can read descriptions and
    decide — that's the whole point of the discovery tool. Only
    truly-empty results return the 'note' branch."""
    bridge = SkillToolProvider(SkillRegistry())
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "xyzzy_nonsense_nothing_matches"},
        provenance="test",
    ))
    assert res.ok is True
    assert res.content["matches"] == []
    assert "No skills matched" in res.content["note"]


@pytest.mark.asyncio
async def test_browse_returns_zero_score_skills_for_inspection() -> None:
    """When the query has tokens but none score > 0 against any
    registered skill, browse still returns them (sorted by score
    desc, all at 0) so the LLM can READ the descriptions and pick.
    This is the design difference from the auto-prefilter, which
    DROPS score=0 to keep the LLM's tool list lean — browse is
    explicitly a 'show me what's there' tool."""
    reg = _registry_with_skills(
        ("git-commit", "Run git commit."),
        ("read-file", "Read a file from disk."),
    )
    bridge = SkillToolProvider(reg)
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "xyzzy_nonsense"},
        provenance="test",
    ))
    assert res.ok is True
    matches = res.content["matches"]
    # Both skills surface at score=0 so the LLM can still read them.
    assert len(matches) == 2
    for m in matches:
        assert m["score"] == 0.0


@pytest.mark.asyncio
async def test_browse_empty_query_errors() -> None:
    """The schema marks query required, but a defensive check in
    the handler still rejects an empty string so a buggy caller
    gets a clear error rather than 'whatever was at the top of
    the registry'."""
    bridge = SkillToolProvider(SkillRegistry())
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": ""},
        provenance="test",
    ))
    assert res.ok is False
    assert "non-empty 'query'" in (res.error or "")


@pytest.mark.asyncio
async def test_browse_respects_top_k_cap() -> None:
    """top_k is hard-capped at 25 so a malicious / hallucinated
    request can't dump the whole catalog into the next turn's
    context."""
    pairs = [(f"skill-{i}", f"placeholder skill {i}") for i in range(50)]
    reg = _registry_with_skills(*pairs)
    bridge = SkillToolProvider(reg)
    # Ask for 999 — should clamp to 25.
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "placeholder", "top_k": 999},
        provenance="test",
    ))
    assert res.ok is True
    assert len(res.content["matches"]) <= 25


@pytest.mark.asyncio
async def test_browse_default_top_k_is_8() -> None:
    """Default top_k=8 on the runtime side keeps the response
    cheap when the LLM doesn't specify."""
    pairs = [(f"placeholder-{i}", "filler description") for i in range(20)]
    reg = _registry_with_skills(*pairs)
    bridge = SkillToolProvider(reg)
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "filler"},
        provenance="test",
    ))
    assert res.ok is True
    assert len(res.content["matches"]) == 8


@pytest.mark.asyncio
async def test_browse_invalid_top_k_falls_back_to_default() -> None:
    """A non-int top_k (e.g. LLM passes a string) shouldn't
    raise — coerce and clamp."""
    reg = _registry_with_skills(
        ("git-commit", "Run git commit."),
    )
    bridge = SkillToolProvider(reg)
    res = await bridge.invoke(ToolCall(
        name=META_BROWSE_TOOL_NAME,
        args={"query": "git", "top_k": "not-a-number"},
        provenance="test",
    ))
    assert res.ok is True


# ── prefilter: meta-tool always passes through ─────────────────────


def test_prefilter_keeps_browse_when_no_match() -> None:
    """B-299 invariant: the prefilter must never strip
    skill_browse, even when the query matches zero registered
    skills. Pre-B-299 the LLM saw zero skill_* on a CJK query;
    now it always sees at least skill_browse."""
    # Build 35 skills (above min_skills_to_filter=30) with English
    # descriptions; query is pure-CJK so token overlap is zero.
    pairs = [(f"english-skill-{i}", f"english description {i}")
             for i in range(35)]
    reg = _registry_with_skills(*pairs)
    bridge = SkillToolProvider(reg)
    all_specs = bridge.list_tools()
    filtered = select_relevant_skills("天气", all_specs, top_k=12)
    names = [s.name for s in filtered]
    assert META_BROWSE_TOOL_NAME in names
    # And NO skill_english-skill-* should be in the filtered list
    # (they all scored 0) — so the LLM sees only meta + non-skill.
    leaked = [n for n in names if n.startswith("skill_english-")]
    assert leaked == [], (
        "prefilter passed zero-score skills through — meta-tool "
        "is supposed to be the only discovery affordance for the "
        "0-match case"
    )


def test_prefilter_keeps_browse_when_match_exists() -> None:
    """When there ARE matches, browse stays in the list AND the
    matched skills also show up. Co-existence — meta isn't a
    replacement for direct invocation, it's a fallback."""
    pairs = [(f"misc-{i}", "filler") for i in range(33)]
    pairs.append(("git-commit", "Run git commit on the repo."))
    reg = _registry_with_skills(*pairs)
    bridge = SkillToolProvider(reg)
    all_specs = bridge.list_tools()
    filtered = select_relevant_skills("git commit", all_specs, top_k=12)
    names = [s.name for s in filtered]
    assert META_BROWSE_TOOL_NAME in names
    assert "skill_git-commit" in names


def test_prefilter_keeps_browse_below_filter_threshold() -> None:
    """When skill count is under min_skills_to_filter (default 30),
    the prefilter is a no-op and the meta-tool naturally passes.
    Test is mostly for documentation — pre-existing behaviour
    already correct, just locked down here."""
    pairs = [(f"a-{i}", "") for i in range(5)]
    reg = _registry_with_skills(*pairs)
    bridge = SkillToolProvider(reg)
    all_specs = bridge.list_tools()
    filtered = select_relevant_skills("any query", all_specs, top_k=12)
    assert META_BROWSE_TOOL_NAME in [s.name for s in filtered]
