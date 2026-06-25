from pathlib import Path

import pytest

from xmclaw.core.persona.md_sync import (
    MdSyncPolicy,
    extract_manual_md_memories,
    parse_md_sync_policy,
    sync_manual_md_to_memory,
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


def test_extract_manual_md_memories_skips_auto_blocks_and_fid_lines(tmp_path: Path) -> None:
    (tmp_path / "USER.md").write_text(
        "# User\n\n"
        "- manual preference\n\n"
        "<!-- XMC-AUTO-EXTRACTED:Auto:BEGIN -->\n"
        "- generated fact\n"
        "<!-- XMC-AUTO-EXTRACTED:Auto:END -->\n"
        "- rendered fact <!-- fid:abc123 -->\n",
        encoding="utf-8",
    )

    candidates = extract_manual_md_memories(tmp_path)
    user = next(c for c in candidates if c.basename == "USER.md")

    assert "manual preference" in user.manual_text
    assert "generated fact" not in user.manual_text
    assert "rendered fact" not in user.manual_text


@pytest.mark.asyncio
async def test_sync_manual_md_to_memory_keeps_md_manual_only(tmp_path: Path) -> None:
    svc = _make_service()
    (tmp_path / "USER.md").write_text(
        "# User\n\n- manual preference\n",
        encoding="utf-8",
    )
    await svc.remember(
        "User prefers Chinese status updates.",
        kind="preference",
        scope="user",
        bucket="user_preference",
        confidence=0.95,
    )

    report = await sync_manual_md_to_memory(svc, tmp_path)
    manual = await svc.get_persona_manual("USER.md")
    text = (tmp_path / "USER.md").read_text(encoding="utf-8")

    assert report.written >= 1
    assert manual is not None
    assert "manual preference" in manual.text
    assert "manual preference" in text
    assert "User prefers Chinese status updates." not in text
    assert "<!-- fid:" not in text


@pytest.mark.asyncio
async def test_production_md_contract_is_manual_view_not_fact_projection(tmp_path: Path) -> None:
    """md is an editable prompt/manual view; facts stay in structured memory."""
    svc = _make_service()
    (tmp_path / "LEARNING.md").write_text(
        "# Learning\n\n- 手写规则：不要把未验证失败当经验\n",
        encoding="utf-8",
    )
    await svc.remember(
        "自动事实：某个工具失败后需要换策略",
        kind="lesson",
        scope="project",
        bucket="rules",
        confidence=0.9,
    )

    report = await sync_manual_md_to_memory(svc, tmp_path, render=True)
    manual = await svc.get_persona_manual("LEARNING.md")
    body = (tmp_path / "LEARNING.md").read_text(encoding="utf-8")

    assert report.written >= 1
    assert manual is not None
    assert "手写规则" in manual.text
    assert "手写规则" in body
    assert "自动事实" not in body
    hits = await svc.recall("工具失败换策略", buckets=["rules"], k=5)
    assert any("自动事实" in h.fact.text for h in hits)


@pytest.mark.asyncio
async def test_sync_header_only_file_deletes_manual_row(tmp_path: Path) -> None:
    """Template-only files must not upsert the section header as manual."""
    svc = _make_service()
    # Seed a previous manual row.
    await svc.upsert_persona_manual("USER.md", "old manual content")

    # Now the on-disk file is just a header (bundled template state).
    (tmp_path / "USER.md").write_text("# User\n", encoding="utf-8")

    report = await sync_manual_md_to_memory(svc, tmp_path)
    manual = await svc.get_persona_manual("USER.md")

    assert report.written >= 1
    assert manual is None


@pytest.mark.asyncio
async def test_sync_empty_manual_file_deletes_manual_row(tmp_path: Path) -> None:
    """A completely empty manual section must clear the stored manual row."""
    svc = _make_service()
    await svc.upsert_persona_manual("USER.md", "previous manual content")

    # File is template only, no user prose.
    (tmp_path / "USER.md").write_text("# Identity\n\n", encoding="utf-8")

    report = await sync_manual_md_to_memory(svc, tmp_path)
    manual = await svc.get_persona_manual("USER.md")

    assert report.written >= 1
    assert manual is None


@pytest.mark.asyncio
async def test_sync_non_header_manual_preserves_manual_row(tmp_path: Path) -> None:
    """Any non-header line counts as user-curated content."""
    svc = _make_service()
    (tmp_path / "USER.md").write_text(
        "# User\n\n- real preference bullet\n",
        encoding="utf-8",
    )

    report = await sync_manual_md_to_memory(svc, tmp_path)
    manual = await svc.get_persona_manual("USER.md")

    assert report.written >= 1
    assert manual is not None
    assert "real preference bullet" in manual.text


@pytest.mark.asyncio
async def test_md_sync_policy_can_disable_writes(tmp_path: Path) -> None:
    svc = _make_service()
    (tmp_path / "USER.md").write_text("# User\n\n- do not ingest\n", encoding="utf-8")

    report = await sync_manual_md_to_memory(
        svc,
        tmp_path,
        policy=MdSyncPolicy(enabled=False),
    )

    assert report.mode == "disabled"
    assert report.written == 0
    assert await svc.get_persona_manual("USER.md") is None


@pytest.mark.asyncio
async def test_md_sync_policy_facts_to_view_does_not_ingest_md(tmp_path: Path) -> None:
    svc = _make_service()
    (tmp_path / "USER.md").write_text("# User\n\n- view only\n", encoding="utf-8")

    report = await sync_manual_md_to_memory(
        svc,
        tmp_path,
        policy=MdSyncPolicy(direction="facts_to_view"),
    )

    assert report.mode == "facts_to_view"
    assert report.written == 0
    assert await svc.get_persona_manual("USER.md") is None


def test_parse_md_sync_policy_from_config() -> None:
    policy = parse_md_sync_policy({
        "cognition": {
            "memory_v2": {
                "md_sync": {
                    "enabled": True,
                    "direction": "facts_to_view",
                    "authority": "manual_md",
                }
            }
        }
    })

    assert policy.enabled is True
    assert policy.direction == "facts_to_view"
    assert policy.authority == "manual_md"
