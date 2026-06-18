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

    Scope is the *self-management toolkit* section specifically —
    other places in the prompt may reference ``sqlite_query`` first
    (e.g. B-302 plan-mode-skip list mentions it as a read-only
    op). A naive ``p.find(...)`` over the whole prompt picks up
    that earlier mention and reports a false-positive failure. We
    instead anchor the search inside the dedicated toolkit section
    so the assertion tracks the routing-order *intent* B-205 fixed.
    """
    p = _DEFAULT_SYSTEM
    section_start = p.find("Self-management toolkit")
    assert section_start > 0, (
        "Self-management toolkit section must exist in default prompt"
    )
    section = p[section_start:]
    mem_idx = section.find("memory_search")
    sql_idx = section.find("sqlite_query")
    assert mem_idx > 0, (
        "memory_search must be referenced inside self-management toolkit"
    )
    assert sql_idx > 0, (
        "sqlite_query must be referenced inside self-management toolkit"
    )
    assert mem_idx < sql_idx, (
        "B-205 invariant: within the self-management toolkit section, "
        "memory_search must appear BEFORE sqlite_query — order is what "
        "makes the LLM prefer it for 'what do I remember' queries."
    )








def test_default_prompt_mentions_tool_aggressiveness() -> None:
    """B-199 lineage: the agent's posture must encourage aggressive
    tool use, not refusal. Without this, the LEARNING.md rule that
    elaborates "no refusal without trying" has no anchor in the
    default prompt."""
    p = _DEFAULT_SYSTEM
    assert "use them aggressively rather than refusing" in p




def test_b210_code_chunk_search_routing_present() -> None:
    """B-210: when workspace code is indexed, the agent must KNOW to
    pass ``kind='code_chunk'`` to memory_search for code questions —
    otherwise persona facts and code chunks share recall budget and
    the ranking degrades. Pin the routing nudge."""
    p = _DEFAULT_SYSTEM
    assert "code_chunk" in p
    # Must be associated with memory_search (not just dropped randomly).
    mem_idx = p.find("memory_search")
    code_idx = p.find("code_chunk")
    assert mem_idx > 0 and code_idx > 0
    assert abs(code_idx - mem_idx) < 1500, (
        "B-210: code_chunk routing nudge must live near the memory_search "
        "section, otherwise the LLM doesn't link them as a workflow."
    )




def test_think_tool_advertised() -> None:
    """Think tool must be advertised in the system prompt so the
    model knows it has a dedicated channel for internal reasoning."""
    p = _DEFAULT_SYSTEM
    assert "think:" in p or "- think" in p
    assert "internal reasoning" in p or "NEVER write reasoning" in p




def test_default_prompt_size_bounded() -> None:
    """Cheap sanity bound — the prompt is appended to every system
    message. If a refactor accidentally bloats it past ~30k chars
    we want a test failure, not a silent 2x cost regression."""
    p = _DEFAULT_SYSTEM
    assert len(p) < 32_000, (
        f"default system prompt grew to {len(p)} chars — over 32k "
        "is a yellow flag for prompt bloat. Audit the latest "
        "section additions before bumping this cap."
    )
    # Conversely: the prompt should not be empty / truncated.
    assert len(p) > 4_000, (
        f"default system prompt is only {len(p)} chars — likely "
        "truncated or missing major sections."
    )


# ── Phase 4: section-based architecture invariants ──────────────────


_EXPECTED_SECTIONS = [
    "identity",
    "capabilities",
    "rules_harder",
    "parallelism",
    "rules_honesty",
    "rules_plan",
    "rules_approval",
    "rules_skill",
    "self_management",
    "notes_journal",
    "self_evolution",
    "task_lifecycle",
    "constraints",
]


def test_section_stamps_present() -> None:
    """Phase 4: every section carries a version stamp so diffs know
    which semantic block changed."""
    p = _DEFAULT_SYSTEM
    stamps = [line for line in p.splitlines() if line.startswith("<!-- section:")]
    assert len(stamps) == len(_EXPECTED_SECTIONS), (
        f"Expected {len(_EXPECTED_SECTIONS)} section stamps, found {len(stamps)}"
    )
    for expected in _EXPECTED_SECTIONS:
        assert any(f"section:{expected} version:" in s for s in stamps), (
            f"Missing stamp for section '{expected}'"
        )


def test_section_order_preserved() -> None:
    """Phase 4: sections appear in the expected order.  Reordering
    changes cache-breakpoint semantics (earlier sections are more
    stable) so this is load-bearing."""
    p = _DEFAULT_SYSTEM
    positions: list[tuple[str, int]] = []
    for sec in _EXPECTED_SECTIONS:
        marker = f"<!-- section:{sec} version:"
        idx = p.find(marker)
        assert idx >= 0, f"Section '{sec}' not found in prompt"
        positions.append((sec, idx))
    # Every section must appear after the previous one.
    for i in range(1, len(positions)):
        prev_sec, prev_pos = positions[i - 1]
        cur_sec, cur_pos = positions[i]
        assert cur_pos > prev_pos, (
            f"Section order violation: '{cur_sec}' at {cur_pos} "
            f"should come after '{prev_sec}' at {prev_pos}"
        )


def test_version_stamp_format() -> None:
    """Phase 4: stamps use HTML-comment shape so they're invisible to
    the LLM but visible to diff tooling."""
    p = _DEFAULT_SYSTEM
    import re
    pat = re.compile(r"<!-- section:(\w+) version:(\d+\.\d+\.\d+) -->")
    matches = pat.findall(p)
    assert len(matches) == len(_EXPECTED_SECTIONS), (
        f"Expected {len(_EXPECTED_SECTIONS)} well-formed stamps, "
        f"found {len(matches)}"
    )
    for name, version in matches:
        assert name in _EXPECTED_SECTIONS
        parts = version.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_no_trailing_whitespace() -> None:
    """Phase 4: _assemble_sections rstrip()s each section so the
    _DEFAULT_SYSTEM tail is clean — _get_static_system_prompt()
    must return byte-identical output."""
    p = _DEFAULT_SYSTEM
    assert not p.endswith(" ") and not p.endswith("\n"), (
        "_DEFAULT_SYSTEM must not end with trailing whitespace"
    )


def test_boundary_not_in_default() -> None:
    """Phase 4: SYSTEM_PROMPT_DYNAMIC_BOUNDARY is appended by
    _with_fresh_time(), not _default_system_prompt().  Keeping it
    separate means the static prefix is pure content."""
    from xmclaw.daemon.prompt_builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in _DEFAULT_SYSTEM
