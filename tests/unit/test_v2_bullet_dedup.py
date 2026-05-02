"""B-183 — bullet dedup helpers (exact + fuzzy).

The persona-file write path (``_append_under_section``) used to do
strict-equality dedup via ``_bullet_core``. Joint audit (B-179)
showed real-data MEMORY.md still grew duplicates because the LLM
paraphrases the SAME fact across sessions — different prose, same
content.

B-183 adds token-set Jaccard fallback so paraphrased restatements
collapse too. These tests pin both the exact and fuzzy paths plus
the trivial-set guard that prevents over-eager merging of short
bullets that share generic words.
"""
from __future__ import annotations

from xmclaw.providers.tool.builtin import (
    _append_under_section,
    _bullet_core,
    _bullet_token_set,
    _is_fuzzy_duplicate,
)


# ── exact-match (B-23) regression ─────────────────────────────────


def test_bullet_core_strips_date_prefix() -> None:
    assert _bullet_core("- 2026-05-02: hello world") == "hello world"
    assert _bullet_core("- 2026-05-02 12:34:56 UTC: hello world") == "hello world"


def test_bullet_core_normalises_whitespace_and_case() -> None:
    a = _bullet_core("- 2026-05-02:  Hello   WORLD!")
    b = _bullet_core("- 2026-05-02: hello world")
    assert a == b


def test_bullet_core_handles_double_date_prefix_legacy() -> None:
    """Legacy MEMORY.md entries had ``YYYY-MM-DD: YYYY-MM-DD: ...``
    from a pre-fix LLM that prepended its own date. Strip both."""
    assert _bullet_core("- 2026-05-02: 2026-05-02: real content") \
           == _bullet_core("- 2026-05-02: real content")


# ── fuzzy (Jaccard) match — B-183 ────────────────────────────────


def test_token_set_drops_stopwords_and_one_char_tokens() -> None:
    tokens = _bullet_token_set(
        "- 2026-05-02: The agent is using bash to run commands"
    )
    # Stopwords stripped:
    assert "the" not in tokens
    assert "is" not in tokens
    assert "to" not in tokens
    # Content tokens kept:
    assert "agent" in tokens
    assert "bash" in tokens
    assert "run" in tokens
    assert "commands" in tokens


def test_token_set_works_on_chinese() -> None:
    tokens = _bullet_token_set(
        "- 2026-05-02: 我们 在 用 bash 跑 命令"
    )
    # Stopwords stripped:
    assert "我们" not in tokens
    assert "在" not in tokens
    # Content kept (CJK + ASCII mixed):
    assert "bash" in tokens


def test_fuzzy_duplicate_catches_paraphrased_sql_restatement() -> None:
    """The exact case from joint-audit MEMORY.md: same SQL query
    introduced by different prose. Pre-B-183 strict dedup missed
    this; B-183 should catch."""
    a_set = _bullet_token_set(
        "- 2026-05-02: events.db tool_invocation_started 的 name 在 "
        "payload JSON 里, query: SELECT json_extract(payload, '$.name') "
        "AS name, COUNT(*) FROM events WHERE type='tool_invocation_started' "
        "GROUP BY name ORDER BY 2 DESC"
    )
    b_set = _bullet_token_set(
        "- 2026-05-02: events.db tool name 存在 payload JSON 里, "
        "查询语法: SELECT json_extract(payload, '$.name') as name, "
        "COUNT(*) FROM events WHERE type='tool_invocation_started' "
        "GROUP BY name ORDER BY call_count DESC"
    )
    # Cores differ (prose), so strict dedup misses; fuzzy must catch.
    assert _bullet_core(str(a_set)) != _bullet_core(str(b_set))
    assert _is_fuzzy_duplicate(a_set, b_set)


def test_fuzzy_duplicate_does_not_match_unrelated_bullets() -> None:
    a = _bullet_token_set("- 2026-05-02: agent picked skill_git-commit autonomously")
    b = _bullet_token_set("- 2026-05-02: events.db has 14000 events of various types")
    assert not _is_fuzzy_duplicate(a, b)


def test_fuzzy_duplicate_skips_short_bullets() -> None:
    """Bullets with < 4 unique content tokens are too small for
    Jaccard to be meaningful — fall back to strict equality only.
    Without this guard, two-word bullets that share one generic
    token would collapse incorrectly."""
    a = _bullet_token_set("- 2026-05-02: use python")
    b = _bullet_token_set("- 2026-05-02: like python")
    # Both have just {use/like, python} after stopword filter — Jaccard
    # would be 1/3 = 0.33, below threshold anyway, but we want to
    # confirm trivial-set guard keeps them un-merged.
    assert not _is_fuzzy_duplicate(a, b)


# ── _append_under_section integration ────────────────────────────


def test_append_skips_exact_duplicate() -> None:
    existing = (
        "# MEMORY\n\n"
        "## Auto-extracted\n\n"
        "- 2026-05-02: agent prefers Python over Go\n"
    )
    out = _append_under_section(
        existing,
        section_header="## Auto-extracted",
        bullet="- 2026-05-03: agent prefers Python over Go",
        placeholder_title="MEMORY",
    )
    # No new bullet written, dates differ but core is identical.
    assert out.count("Python over Go") == 1


def test_append_skips_fuzzy_duplicate() -> None:
    """B-183 — paraphrased restatement collapses despite wording diff."""
    existing = (
        "# MEMORY\n\n"
        "## Tooling internals\n\n"
        "- 2026-05-02: events.db tool_invocation_started 的 name 在 payload "
        "JSON, query SELECT json_extract(payload, '$.name') AS name, "
        "COUNT(*) FROM events WHERE type='tool_invocation_started' "
        "GROUP BY name ORDER BY 2 DESC\n"
    )
    out = _append_under_section(
        existing,
        section_header="## Tooling internals",
        bullet=(
            "- 2026-05-02: events.db tool name 存在 payload JSON 里，"
            "查询语法 SELECT json_extract(payload, '$.name') as name, "
            "COUNT(*) FROM events WHERE type='tool_invocation_started' "
            "GROUP BY name ORDER BY call_count DESC"
        ),
        placeholder_title="MEMORY",
    )
    bullet_count = sum(
        1 for line in out.splitlines()
        if line.startswith("- ") and "json_extract" in line.lower()
    )
    assert bullet_count == 1, (
        "fuzzy dedup didn't collapse paraphrased SQL restatement"
    )


def test_append_writes_genuinely_new_bullet() -> None:
    """Non-duplicate bullets still land — make sure dedup isn't too eager."""
    existing = (
        "# MEMORY\n\n"
        "## Auto-extracted\n\n"
        "- 2026-05-02: agent prefers Python over Go\n"
    )
    out = _append_under_section(
        existing,
        section_header="## Auto-extracted",
        bullet="- 2026-05-02: build always breaks when setuptools missing",
        placeholder_title="MEMORY",
    )
    assert "Python over Go" in out
    assert "setuptools missing" in out
