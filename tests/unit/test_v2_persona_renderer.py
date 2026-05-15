"""Tests for persona MD renderer — Wave-27 fix-12 (refactor B Phase 1).

Reproduces the user's "L1 has 小咪, IDENTITY.md is empty" complaint.
After fact write + render_affected_files, the on-disk MD file must
reflect what L1 holds.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.persona.v2_renderer import (
    BUCKET_TO_FILE,
    FILE_TO_BUCKETS,
    render_affected_files,
    render_all_persona_files,
    render_persona_file,
)
from xmclaw.memory.v2 import (
    EmbeddingService,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
)


def _make_service() -> MemoryService:
    return MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )


# ── Routing table sanity ─────────────────────────────────────────


def test_bucket_to_file_routing_covers_phase_1_buckets():
    """Phase 1 ships routes for the three buckets the LLM extractor
    emits today. Phase 2 will extend to lesson buckets."""
    assert set(BUCKET_TO_FILE) >= {
        "agent_identity", "user_identity", "user_preference",
    }
    # Each entry must be a 3-tuple of strings.
    for bucket, cfg in BUCKET_TO_FILE.items():
        assert len(cfg) == 3
        filename, header, prefix = cfg
        assert filename.endswith(".md")
        assert header.startswith("## ")
        assert isinstance(prefix, str)


def test_file_to_buckets_inverse_consistent():
    """FILE_TO_BUCKETS must invert BUCKET_TO_FILE — every bucket
    appears in the list under its target file."""
    for bucket, (filename, _, _) in BUCKET_TO_FILE.items():
        assert bucket in FILE_TO_BUCKETS[filename]


# ── render_persona_file ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_writes_identity_md_from_agent_identity_facts(
    tmp_path: Path,
):
    """The exact reproducer for the user's 2026-05-15 complaint:
    LLM extractor wrote ``kind=identity, scope=session`` (agent's
    self-id) with ``bucket=agent_identity``. IDENTITY.md should
    now render with that fact in the Auto-extracted section.
    """
    svc = _make_service()
    await svc.remember(
        "AI 的名字是小咪",
        kind="identity", scope="session",
        bucket="agent_identity",
        confidence=0.95,
    )
    wrote = await render_persona_file(svc, tmp_path, "IDENTITY.md")
    assert wrote is True

    content = (tmp_path / "IDENTITY.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in content
    assert "AI 的名字是小咪" in content
    assert "conf 0.95" in content


@pytest.mark.asyncio
async def test_render_writes_user_md_combining_identity_and_preference(
    tmp_path: Path,
):
    """USER.md aggregates TWO bucket queries: identity facts about
    the user (``user_identity``) AND user-scope preferences
    (``user_preference``). Both must show up in distinct sections.
    """
    svc = _make_service()
    await svc.remember(
        "用户哥的称呼是哥或敬宇",
        kind="identity", scope="user",
        bucket="user_identity",
    )
    await svc.remember(
        "用户偏好简洁直接的回复",
        kind="preference", scope="user",
        bucket="user_preference",
    )
    await render_persona_file(svc, tmp_path, "USER.md")
    content = (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "## Auto-identity" in content
    assert "## Auto-extracted preferences" in content
    assert "用户哥的称呼是哥或敬宇" in content
    assert "用户偏好简洁直接的回复" in content


@pytest.mark.asyncio
async def test_render_unknown_basename_skipped(tmp_path: Path):
    """Basenames without a bucket mapping (e.g. BOOTSTRAP.md, which
    is bootstrap-interview-only and never auto-rendered) are
    silently skipped — the renderer returns False without touching
    disk."""
    svc = _make_service()
    wrote = await render_persona_file(svc, tmp_path, "BOOTSTRAP.md")
    assert wrote is False
    assert not (tmp_path / "BOOTSTRAP.md").exists()


@pytest.mark.asyncio
async def test_render_idempotent_no_changes_returns_false(tmp_path: Path):
    """Re-rendering with identical L1 state must NOT rewrite the
    file — Phase 1 cares about avoiding hot-path I/O when nothing
    actually changed.
    """
    svc = _make_service()
    await svc.remember(
        "AI 的名字是小咪", kind="identity", scope="session",
        bucket="agent_identity",
    )
    first = await render_persona_file(svc, tmp_path, "IDENTITY.md")
    second = await render_persona_file(svc, tmp_path, "IDENTITY.md")
    assert first is True
    assert second is False  # no-op


@pytest.mark.asyncio
async def test_render_empty_section_emits_placeholder(tmp_path: Path):
    """When no facts match the bucket, the section header still
    appears with a "(nothing extracted yet)" placeholder — the
    UI / human reader sees the section exists.
    """
    svc = _make_service()
    # No matching facts written.
    await render_persona_file(svc, tmp_path, "IDENTITY.md")
    content = (tmp_path / "IDENTITY.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in content
    assert "nothing extracted yet" in content


# ── render_affected_files ────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_affected_only_touches_files_with_changed_buckets(
    tmp_path: Path,
):
    """If the just-written batch contains ONLY user_preference
    facts, only USER.md gets touched — IDENTITY.md is not opened.
    """
    svc = _make_service()
    f = await svc.remember(
        "用户偏好直白回复",
        kind="preference", scope="user",
        bucket="user_preference",
    )
    affected = await render_affected_files(svc, tmp_path, [f])
    assert affected == {"USER.md"}
    assert (tmp_path / "USER.md").exists()
    assert not (tmp_path / "IDENTITY.md").exists()


@pytest.mark.asyncio
async def test_render_affected_skips_facts_without_bucket(tmp_path: Path):
    """Facts written without a bucket label (e.g. project URLs from
    KeyInfoExtractor's URL pattern) don't trigger any render."""
    svc = _make_service()
    f = await svc.remember(
        "网址: https://example.com",
        kind="project", scope="project",
        # bucket="" implicit
    )
    affected = await render_affected_files(svc, tmp_path, [f])
    assert affected == set()


# ── render_all_persona_files ─────────────────────────────────────


@pytest.mark.asyncio
async def test_render_all_processes_every_routed_file(tmp_path: Path):
    """Bulk-render path renders every file in FILE_TO_BUCKETS, used
    by the migration utility / debug endpoint."""
    svc = _make_service()
    await svc.remember(
        "AI 叫小咪", kind="identity", scope="session",
        bucket="agent_identity",
    )
    await svc.remember(
        "用户叫哥", kind="identity", scope="user",
        bucket="user_identity",
    )
    report = await render_all_persona_files(svc, tmp_path)
    # Every routed file appears in the report.
    assert set(report) == set(FILE_TO_BUCKETS)
    # IDENTITY.md + USER.md both got written.
    assert report["IDENTITY.md"] is True
    assert report["USER.md"] is True


# ── Phase 2: lesson bucket routes ────────────────────────────────


def test_phase2_routes_cover_five_lesson_buckets():
    """All five lesson buckets ExtractLessonsHook emits today must
    map to their corresponding persona MD file. Header strings
    must match the legacy AUTO_SECTIONS so the agent's read path
    sees consistent section names regardless of which renderer
    wrote the file.
    """
    expected = {
        "workflow":      ("AGENTS.md",   "## Auto-extracted"),
        "tool_quirks":   ("TOOLS.md",    "## Auto-extracted"),
        "failure_modes": ("MEMORY.md",   "## Failure Modes"),
        "values":        ("SOUL.md",     "## Auto-extracted"),
        "rules":         ("LEARNING.md", "## Auto-extracted"),
    }
    for bucket, (file, header) in expected.items():
        assert bucket in BUCKET_TO_FILE, (
            f"Phase 2 bucket {bucket!r} missing from BUCKET_TO_FILE"
        )
        got_file, got_header, _ = BUCKET_TO_FILE[bucket]
        assert got_file == file
        assert got_header == header


@pytest.mark.asyncio
async def test_render_writes_soul_md_from_values_bucket(tmp_path: Path):
    """The SOUL.md case the user explicitly worried about.

    User feedback: "他对话时读取的也有 soul"  →  must not break.
    After ExtractLessonsHook produces a ``values`` lesson, SOUL.md
    auto section renders from v2 — agent's next-turn read of
    SOUL.md sees fresh content.
    """
    svc = _make_service()
    await svc.remember(
        "诚实优于完美的形象",
        kind="lesson", scope="project",
        bucket="values",
    )
    wrote = await render_persona_file(svc, tmp_path, "SOUL.md")
    assert wrote is True
    content = (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in content
    assert "诚实优于完美的形象" in content


@pytest.mark.asyncio
async def test_render_agents_tools_memory_learning_each_from_own_bucket(
    tmp_path: Path,
):
    """Pin the 1-to-1 mapping: each lesson bucket → its single
    target MD file. Touching one bucket must not bleed into
    another file.
    """
    svc = _make_service()
    cases = [
        ("workflow",      "AGENTS.md",   "grep before read"),
        ("tool_quirks",   "TOOLS.md",    "bash on Windows = Git Bash"),
        ("failure_modes", "MEMORY.md",   "session restore replays"),
        ("rules",         "LEARNING.md", "if zero skill match → skill_browse"),
    ]
    for bucket, _, text in cases:
        # skip_contradict_check=True bypasses the write-time
        # near-dup merge, which StubEmbedder triggers too eagerly
        # for short distinct test strings (real qwen-embedding-0.6b
        # at d=1024 would separate these cleanly).
        await svc.remember(
            text, kind="lesson", scope="project", bucket=bucket,
            skip_contradict_check=True,
        )
    report = await render_all_persona_files(svc, tmp_path)
    for bucket, expected_file, text in cases:
        assert report[expected_file] is True, (
            f"{expected_file} not rendered for bucket {bucket!r}"
        )
        body = (tmp_path / expected_file).read_text(encoding="utf-8")
        assert text in body, (
            f"{text!r} missing from {expected_file}"
        )
    # Cross-check: workflow text should NOT leak into TOOLS.md, etc.
    agents_body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    tools_body = (tmp_path / "TOOLS.md").read_text(encoding="utf-8")
    assert "bash on Windows" not in agents_body
    assert "grep before read" not in tools_body


@pytest.mark.asyncio
async def test_affected_files_path_handles_lesson_buckets(tmp_path: Path):
    """The hot-path entry — ``render_affected_files`` — must
    recognise lesson buckets and render the right MD file."""
    svc = _make_service()
    f = await svc.remember(
        "诚实优于完美的形象",
        kind="lesson", scope="project",
        bucket="values",
    )
    affected = await render_affected_files(svc, tmp_path, [f])
    assert affected == {"SOUL.md"}
    assert (tmp_path / "SOUL.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "USER.md").exists()


# ── Phase 3b: manual section storage in v2 ───────────────────────


@pytest.mark.asyncio
async def test_upsert_persona_manual_is_idempotent_on_basename(
    tmp_path: Path,
):
    """One row per file, regardless of how many times the user
    saves. compute_id keys on basename via the special path, so
    a second save REPLACES the first instead of stacking."""
    svc = _make_service()
    a = await svc.upsert_persona_manual("IDENTITY.md", "v1 content")
    b = await svc.upsert_persona_manual("IDENTITY.md", "v2 content")
    assert a.id == b.id, "manual id must be deterministic on basename"
    refetched = await svc.get_persona_manual("IDENTITY.md")
    assert refetched is not None
    assert refetched.text == "v2 content"


@pytest.mark.asyncio
async def test_render_merges_manual_section_with_auto_sections(
    tmp_path: Path,
):
    """The user's IDENTITY.md case: manual section ("我叫嘉鸿,1995年生")
    + auto section ("AI 叫小咪") rendered together. Both must
    appear, in the documented order (manual first, auto after).
    """
    svc = _make_service()
    await svc.upsert_persona_manual(
        "IDENTITY.md",
        "# IDENTITY.md\n用户叫嘉鸿,1995 年生,做电商业务",
    )
    await svc.remember(
        "AI 的名字是小咪",
        kind="identity", scope="session",
        bucket="agent_identity",
    )
    await render_persona_file(svc, tmp_path, "IDENTITY.md")
    content = (tmp_path / "IDENTITY.md").read_text(encoding="utf-8")

    assert "用户叫嘉鸿" in content
    assert "AI 的名字是小咪" in content
    assert "## Auto-extracted" in content
    # Manual section physically precedes auto section.
    assert content.index("用户叫嘉鸿") < content.index("## Auto-extracted")


@pytest.mark.asyncio
async def test_render_manual_only_when_no_auto_buckets(tmp_path: Path):
    """For files like BOOTSTRAP.md that have NO auto buckets
    mapped, the renderer should still write manual content when
    v2 has a row for that file.
    """
    svc = _make_service()
    await svc.upsert_persona_manual(
        "BOOTSTRAP.md",
        "# BOOTSTRAP\n首次启动 interview 笔记",
    )
    wrote = await render_persona_file(svc, tmp_path, "BOOTSTRAP.md")
    assert wrote is True
    content = (tmp_path / "BOOTSTRAP.md").read_text(encoding="utf-8")
    assert "首次启动 interview 笔记" in content


@pytest.mark.asyncio
async def test_render_preserves_disk_manual_when_v2_has_none(
    tmp_path: Path,
):
    """If v2 has no persona_manual row yet (e.g. user has never
    touched UI save), the renderer must NOT wipe whatever was on
    disk's manual section. It just refreshes the auto portion.
    """
    target = tmp_path / "IDENTITY.md"
    target.write_text(
        "# IDENTITY.md\n用户手写内容\n\n## Auto-extracted\n_(nothing yet)_\n",
        encoding="utf-8",
    )
    svc = _make_service()
    await svc.remember(
        "AI 叫小咪",
        kind="identity", scope="session",
        bucket="agent_identity",
    )
    await render_persona_file(svc, tmp_path, "IDENTITY.md")
    content = target.read_text(encoding="utf-8")
    # Manual content survives.
    assert "用户手写内容" in content
    # Auto section got the new fact.
    assert "AI 叫小咪" in content


@pytest.mark.asyncio
async def test_upsert_persona_manual_rejects_empty_basename():
    svc = _make_service()
    with pytest.raises(ValueError):
        await svc.upsert_persona_manual("", "anything")
