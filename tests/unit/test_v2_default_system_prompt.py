"""Default system prompt — structural invariants.

Pin the prompt-shape decisions made during the probe_b200_v2 →
fix loop (B-202..B-205). The prompt is a string + format-string
collage in ``daemon/agent_loop.py``; without these tests a refactor
that re-orders sections or accidentally re-introduces the 4-step
ceremony block would silently regress agent behaviour against
real-data findings.

Each test maps to a specific B-ticket:

* B-204 — 4-step new-skill onboarding ceremony was deleted; the
  prompt should NOT re-introduce a 4-numbered-step section about
  invoking skills.
* B-205 — ``memory_search`` was promoted to first position in the
  self-management toolkit and explicitly preferred over
  ``sqlite_query`` for "what do I remember about X" queries.
* B-199 — "不会做的事 ≠ 真做不到" hard rule lives in LEARNING.md
  (injected via persona, not hard-coded into ``_DEFAULT_SYSTEM``);
  the default prompt should still mention that aggressive tool
  use is the correct posture so the rule has somewhere to land.
"""
from __future__ import annotations

from xmclaw.daemon.agent_loop import _DEFAULT_SYSTEM


def test_b205_memory_search_appears_before_sqlite_query() -> None:
    """B-205: probe data showed 47 sqlite_query / 0 memory_search.
    The fix was prompt-only — promoting memory_search to first
    position in the self-management toolkit. Order matters because
    the LLM picks the first-listed tool that fits the query.
    """
    p = _DEFAULT_SYSTEM
    mem_idx = p.find("memory_search")
    sql_idx = p.find("sqlite_query")
    assert mem_idx > 0, "memory_search must be referenced in default prompt"
    assert sql_idx > 0, "sqlite_query must be referenced in default prompt"
    assert mem_idx < sql_idx, (
        "B-205 invariant: memory_search must appear BEFORE sqlite_query "
        "in the default prompt — order is what makes the LLM prefer it."
    )


def test_b205_memory_search_marked_first_line_tool() -> None:
    """B-205: not just first by position, also explicitly marked
    "first-line tool" so the LLM sees the routing intent."""
    p = _DEFAULT_SYSTEM
    assert "first-line tool" in p, (
        "B-205: memory_search section must call itself out as "
        "'first-line tool' to make the routing decision explicit."
    )


def test_b205_sqlite_query_scope_narrowed() -> None:
    """B-205: sqlite_query was demoted to "structural / quantitative"
    questions only, with explicit redirect to memory_search for
    semantic recall."""
    p = _DEFAULT_SYSTEM
    # The redirect line should be present.
    assert "use `memory_search` instead" in p, (
        "B-205: sqlite_query block must redirect 'what do I know "
        "about <topic>' to memory_search."
    )


def test_b204_no_four_step_ceremony_for_skills() -> None:
    """B-204: the 4-step new-skill ceremony was the cause of 3/40
    skill invocation rate. The replacement is a 1-step "read desc
    + invoke" default. Pin: the deleted block must not return."""
    p = _DEFAULT_SYSTEM
    # Old wording should be gone.
    assert "4-step learning workflow" not in p
    assert "Walk through these 4 steps in order" not in p
    # New header should be present.
    assert "Using skills" in p


def test_default_prompt_mentions_tool_aggressiveness() -> None:
    """B-199 lineage: the agent's posture must encourage aggressive
    tool use, not refusal. Without this, the LEARNING.md rule that
    elaborates "no refusal without trying" has no anchor in the
    default prompt."""
    p = _DEFAULT_SYSTEM
    assert "use them aggressively rather than refusing" in p


def test_b206_narration_discipline_present() -> None:
    """B-206: probe data showed MiniMax M2.7 / OpenAI-compat models
    emit empty content on intermediate hops, leaving the user staring
    at silent tool cards between hop 0 narration and the final
    synthesis. Fix is a hard system-prompt rule. This test pins the
    rule so a refactor doesn't silently delete it."""
    p = _DEFAULT_SYSTEM
    assert "Narration discipline" in p
    # Must explicitly call out the OpenAI-compat model class —
    # Anthropic users don't need this rule, but the rule must
    # survive even if the user runs Claude (it's a no-op there).
    assert "OpenAI-compatible" in p or "MiniMax" in p
    # The "before next tool call" framing is the load-bearing part;
    # without it the rule degrades to "narrate sometimes".
    assert "BEFORE emitting the next tool call" in p


def test_default_prompt_size_bounded() -> None:
    """Cheap sanity bound — the prompt is appended to every system
    message. If a refactor accidentally bloats it past ~30k chars
    we want a test failure, not a silent 2x cost regression."""
    p = _DEFAULT_SYSTEM
    assert len(p) < 30_000, (
        f"default system prompt grew to {len(p)} chars — over 30k "
        "is a yellow flag for prompt bloat. Audit the latest "
        "section additions before bumping this cap."
    )
    # Conversely: the prompt should not be empty / truncated.
    assert len(p) > 4_000, (
        f"default system prompt is only {len(p)} chars — likely "
        "truncated or missing major sections."
    )
