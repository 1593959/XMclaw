"""Phase 1 of memory v3 — pins the BUCKETS registry schema.

If you're tempted to add a bucket / rename one / change its target
file, update these tests in the SAME commit so the migration story
(test_v3_dark_facts_migration) catches data that needs backfill.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2 import buckets as bm


# ─── Coverage: every legacy + new bucket present ──────────────────


REQUIRED_BUCKETS = {
    # Original 8 from the v2 v2_renderer dict.
    "agent_identity", "user_identity", "user_preference",
    "workflow", "tool_quirks", "failure_modes",
    "values", "rules",
    # New in v3 (close the coverage gap).
    "project_fact", "commitment", "environment", "misc",
}


def test_required_buckets_registered():
    assert REQUIRED_BUCKETS.issubset(bm.BUCKETS.keys()), (
        f"missing buckets: "
        f"{REQUIRED_BUCKETS - bm.BUCKETS.keys()}"
    )


def test_default_bucket_is_misc():
    """The catch-all that closes the dark-fact loophole. If this is
    renamed without updating MemoryService.remember, every legacy
    writer's empty-bucket path silently breaks."""
    assert bm.DEFAULT_BUCKET == "misc"
    assert bm.DEFAULT_BUCKET in bm.BUCKETS


def test_every_bucket_has_complete_definition():
    """A registry entry without target_file / section / cap leaks
    into v2_renderer as a runtime crash. Trip the test instead."""
    for tag, b in bm.BUCKETS.items():
        assert b.tag == tag, f"{tag} mismatched .tag={b.tag!r}"
        assert b.target_file.endswith(".md"), b.target_file
        assert b.section.startswith("## "), b.section
        assert b.cap_chars > 0
        assert b.cap_items > 0
        assert b.default_kind
        assert b.description, f"{tag} missing description"


def test_section_slugs_are_unique_within_a_file():
    """Two buckets pointing at the same file with the same section
    would collide in v2_renderer's marker block. Catch at registry
    load time."""
    per_file: dict[str, set[str]] = {}
    for b in bm.BUCKETS.values():
        bucket_sections = per_file.setdefault(b.target_file, set())
        assert b.section not in bucket_sections, (
            f"{b.tag}: section {b.section!r} collides with another "
            f"bucket in {b.target_file}"
        )
        bucket_sections.add(b.section)


# ─── resolve / is_known / for_file ────────────────────────────────


def test_resolve_known_returns_bucket():
    b = bm.resolve("user_preference")
    assert b.tag == "user_preference"
    assert b.target_file == "USER.md"


def test_resolve_empty_returns_misc():
    """The dark-fact closer. Empty string or None MUST resolve to
    misc, NEVER raise, NEVER return None."""
    for empty in ("", None):
        b = bm.resolve(empty)
        assert b.tag == "misc"
        assert b.target_file == "MEMORY.md"


def test_resolve_unknown_returns_misc():
    """LLM might hallucinate a bucket name. Should land in misc
    rather than crash the extractor / persona writer."""
    b = bm.resolve("definitely_not_a_real_bucket_xyz")
    assert b.tag == "misc"


def test_is_known_strict():
    assert bm.is_known("user_preference") is True
    assert bm.is_known("misc") is True
    # Unknown / empty / None are all False — caller knows to coerce.
    assert bm.is_known("") is False
    assert bm.is_known(None) is False
    assert bm.is_known("garbage") is False


def test_for_file_aggregates_buckets_correctly():
    """USER.md aggregates user_identity + user_preference, MEMORY.md
    aggregates failure_modes + project_fact + commitment + misc."""
    user_buckets = {b.tag for b in bm.for_file("USER.md")}
    assert user_buckets == {"user_identity", "user_preference"}

    memory_buckets = {b.tag for b in bm.for_file("MEMORY.md")}
    assert memory_buckets == {
        "failure_modes", "environment", "project_fact", "commitment", "misc",
    }


def test_known_files_covers_seven_persona_files():
    """The persona layer was built around 7 .md files. The registry
    should map into a subset of these (or all of them once Phase 2
    closes the coverage gap on AGENTS/TOOLS/SOUL/LEARNING)."""
    expected_subset = {
        "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md",
        "MEMORY.md", "SOUL.md", "LEARNING.md",
    }
    assert set(bm.known_files()).issubset(expected_subset)
    # And NONE of them is missing (Phase 1 closes this gap).
    assert set(bm.known_files()) == expected_subset


# ─── render_for_prompt ────────────────────────────────────────────


def test_render_for_prompt_includes_every_bucket():
    """Drives the LLM extractor prompt. Missing bucket = silent
    classification miss."""
    rendered = bm.render_for_prompt()
    for tag in bm.BUCKETS:
        assert tag in rendered, f"{tag} missing from prompt render"
    # Must also tell the LLM about the misc fallback explicitly so
    # the model doesn't invent its own.
    assert "misc" in rendered


def test_render_for_prompt_is_stable_under_repeated_calls():
    """Cheap pure function — used inside the LLM call hot path."""
    a = bm.render_for_prompt()
    b = bm.render_for_prompt()
    assert a == b


# ─── Safe section slug ────────────────────────────────────────────


def test_safe_section_slug_is_alnum_only():
    """v2_renderer composes HTML comments from this — slugs with
    angle brackets / dashes / spaces would break the markers."""
    for b in bm.BUCKETS.values():
        slug = b.safe_section_slug
        # Allowed chars: alphanumeric (Unicode letters/digits) +
        # hyphens. Caller wraps in <!-- ... -->; anything outside
        # this set breaks the marker pattern.
        for ch in slug:
            assert ch.isalnum() or ch == "-", (
                f"{b.tag}: slug {slug!r} has invalid char {ch!r}"
            )
