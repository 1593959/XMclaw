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
    """Basenames without a bucket mapping (e.g. SOUL.md — covered
    in Phase 2) are silently skipped — the renderer returns False
    without touching disk."""
    svc = _make_service()
    wrote = await render_persona_file(svc, tmp_path, "SOUL.md")
    assert wrote is False
    assert not (tmp_path / "SOUL.md").exists()


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
