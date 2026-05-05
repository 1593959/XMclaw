"""B-198 / Phase 3: PersonaStore unit tests.

Locks the contract:

* Manual prose stored as one persona_manual row per file
* Auto-section rendered from extracted facts of the matching kind
* Fact rows merged via upsert_fact (B-197 Phase 2 strengthen behavior)
* Render output = manual + (auto-section block when facts exist)
* set_manual strips any auto-section text the caller passed in
* migrate_from_disk reads existing markdown, splits, seeds rows
* Disk render uses atomic_write (proven by file-presence tests)

Uses an in-memory ``SqliteVecMemory`` so the contract is tested end-to-end
through the same store the daemon ships with — not via mock provider.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.core.persona.store import (
    AUTO_SECTIONS,
    PersonaStore,
    parse_auto_bullets,
    split_manual_and_auto,
)
from xmclaw.providers.memory.base import MemoryItem
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory


def _factory(**kw):
    return MemoryItem(**kw)


@pytest.fixture
def mem() -> SqliteVecMemory:
    return SqliteVecMemory(":memory:")


@pytest.fixture
def store(mem: SqliteVecMemory, tmp_path: Path) -> PersonaStore:
    pdir = tmp_path / "persona"
    pdir.mkdir()
    return PersonaStore(mem, pdir, item_factory=_factory)


# ── markdown split helpers ─────────────────────────────────────────


def test_split_manual_and_auto_with_header_present() -> None:
    text = (
        "# USER.md — about me\n\n"
        "- name: 张伟\n"
        "- timezone: CST\n\n"
        "## Auto-extracted preferences\n\n"
        "- [auto] uses Chinese\n"
    )
    manual, auto = split_manual_and_auto(
        text, auto_header="## Auto-extracted preferences",
    )
    assert "## Auto-extracted preferences" not in manual
    assert "name: 张伟" in manual
    assert auto.startswith("## Auto-extracted preferences")
    assert "[auto] uses Chinese" in auto


def test_split_no_auto_header_returns_full_text_as_manual() -> None:
    text = "# SOUL.md\n\nQuiet, helpful, direct.\n"
    manual, auto = split_manual_and_auto(text, auto_header="## Auto-extracted")
    assert manual.strip() == text.strip()
    assert auto == ""


def test_parse_auto_bullets_picks_only_top_level_bullets() -> None:
    auto = (
        "## Auto-extracted\n\n"
        "- one\n"
        "- two\n"
        "  - nested (skipped — not top-level)\n"
        "Some prose paragraph (skipped).\n"
        "- three\n"
    )
    bullets = parse_auto_bullets(auto)
    assert bullets == ["one", "two", "three"]


# ── basic round-trip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_manual_then_get_text_roundtrips(store: PersonaStore) -> None:
    await store.set_manual("SOUL.md", "Quiet, direct.\n")
    out = await store.get_text("SOUL.md")
    assert out.strip() == "Quiet, direct."


@pytest.mark.asyncio
async def test_set_manual_strips_auto_section_caller_included(
    store: PersonaStore,
) -> None:
    """When the caller round-trips a render through Web UI without
    edits, the file body includes the auto section. set_manual must
    drop it — the manual row stays clean, auto is derived."""
    text = (
        "# USER.md\n\n"
        "- name: 张伟\n\n"
        "## Auto-extracted preferences\n\n"
        "- [×3] uses Chinese\n"
    )
    await store.set_manual("USER.md", text)
    # Read it back via _read_manual directly to skip the auto append.
    manual = await store._read_manual("USER.md")
    assert "Auto-extracted preferences" not in manual
    assert "[×3]" not in manual
    assert "name: 张伟" in manual


# ── auto-section render ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_section_renders_preferences_under_user_md(
    store: PersonaStore,
) -> None:
    await store.set_manual("USER.md", "# About me\n- 张伟\n")
    await store.add_fact(
        kind="preference", text="uses Chinese",
        metadata={"kind": "preference"},
    )
    await store.add_fact(
        kind="preference", text="terse replies",
        metadata={"kind": "preference"},
    )
    out = await store.get_text("USER.md")
    assert "## Auto-extracted preferences" in out
    assert "uses Chinese" in out
    assert "terse replies" in out
    assert "# About me" in out  # manual still on top


@pytest.mark.asyncio
async def test_lessons_route_to_correct_files_by_bucket(
    store: PersonaStore,
) -> None:
    """B-198: lessons with bucket=workflow → AGENTS.md, bucket=
    tool_quirks → TOOLS.md, bucket=failure_modes → MEMORY.md."""
    await store.add_fact(
        kind="lesson", text="grep before reading large files",
        metadata={"kind": "lesson", "bucket": "workflow"},
    )
    await store.add_fact(
        kind="lesson", text="ruff lints static js — exclude it",
        metadata={"kind": "lesson", "bucket": "tool_quirks"},
    )
    await store.add_fact(
        kind="lesson", text="cron run_once=true creates new sid every tick",
        metadata={"kind": "lesson", "bucket": "failure_modes"},
    )
    agents = await store.get_text("AGENTS.md")
    tools = await store.get_text("TOOLS.md")
    memory = await store.get_text("MEMORY.md")
    assert "grep before reading" in agents
    assert "ruff lints static" in tools
    assert "cron run_once" in memory
    # Cross-pollination: workflow lesson should NOT show up in MEMORY.md
    assert "grep before reading" not in memory


@pytest.mark.asyncio
async def test_evidence_count_marker_in_render(store: PersonaStore) -> None:
    """When the same fact gets upserted multiple times, the rendered
    bullet shows ``[×N]`` so humans see it's been re-confirmed."""
    md = {"kind": "preference"}
    await store.add_fact(kind="preference", text="uses Chinese", metadata=md)
    await store.add_fact(kind="preference", text="uses Chinese", metadata=md)
    await store.add_fact(kind="preference", text="uses Chinese", metadata=md)
    out = await store.get_text("USER.md")
    # B-197 Phase 2 upsert merges them — render shows ×3.
    assert "×3" in out
    # And only ONE bullet line, not three.
    assert out.count("uses Chinese") == 1


# ── render to disk ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_to_disk_writes_file_after_set_manual(
    store: PersonaStore, tmp_path: Path,
) -> None:
    await store.set_manual("IDENTITY.md", "I am XMclaw.\n")
    f = store.profile_dir / "IDENTITY.md"
    assert f.is_file()
    assert "I am XMclaw." in f.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_render_to_disk_atomic_write_safety(
    store: PersonaStore,
) -> None:
    """Render-to-disk must always leave a valid file. We check by
    writing twice and reading the second result — atomic_write_text
    handles the safety guarantees, this just verifies the flow."""
    await store.set_manual("LEARNING.md", "v1 content\n")
    await store.set_manual("LEARNING.md", "v2 content\n")
    f = store.profile_dir / "LEARNING.md"
    assert "v2" in f.read_text(encoding="utf-8")
    assert "v1" not in f.read_text(encoding="utf-8")


# ── migration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrate_from_disk_seeds_manual_and_auto_rows(
    store: PersonaStore, tmp_path: Path,
) -> None:
    pdir = store.profile_dir
    (pdir / "USER.md").write_text(
        "# USER.md\n\n"
        "- name: 张伟\n\n"
        "## Auto-extracted preferences\n\n"
        "- uses Chinese\n"
        "- terse replies\n",
        encoding="utf-8",
    )
    report = await store.migrate_from_disk()
    assert report["USER.md"] == 2  # two bullets migrated

    out = await store.get_text("USER.md")
    assert "name: 张伟" in out
    assert "uses Chinese" in out
    assert "terse replies" in out


@pytest.mark.asyncio
async def test_migrate_from_disk_idempotent(
    store: PersonaStore,
) -> None:
    """Running migrate twice doesn't duplicate rows. We re-render and
    verify exactly one bullet for the same migrated text."""
    pdir = store.profile_dir
    (pdir / "USER.md").write_text(
        "# USER.md\n\n## Auto-extracted preferences\n\n- uses Chinese\n",
        encoding="utf-8",
    )
    await store.migrate_from_disk()
    await store.migrate_from_disk()
    out = await store.get_text("USER.md")
    assert out.count("uses Chinese") == 1


@pytest.mark.asyncio
async def test_migrate_pure_manual_file_no_auto_split(
    store: PersonaStore,
) -> None:
    """SOUL.md has no auto-section config — full file goes to manual."""
    pdir = store.profile_dir
    (pdir / "SOUL.md").write_text(
        "# SOUL\n\nQuiet. Helpful. Direct.\n",
        encoding="utf-8",
    )
    await store.migrate_from_disk()
    out = await store.get_text("SOUL.md")
    assert "Quiet. Helpful. Direct." in out
    # No auto-section header should have appeared.
    assert "Auto-extracted" not in out


# ── error isolation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_basename_in_set_manual_raises(
    store: PersonaStore,
) -> None:
    with pytest.raises(ValueError, match="unknown persona file"):
        await store.set_manual("MY_RANDOM.md", "content")


@pytest.mark.asyncio
async def test_get_text_empty_when_nothing_seeded(
    store: PersonaStore,
) -> None:
    """Fresh-install signal: no manual + no facts = empty string."""
    out = await store.get_text("USER.md")
    assert out == ""


@pytest.mark.asyncio
async def test_known_persona_files_match_loader(
    store: PersonaStore,
) -> None:
    """The PersonaStore's AUTO_SECTIONS keys must align with the
    persona loader's PERSONA_BASENAMES — otherwise the assembler
    asks for files the store doesn't know about, or vice versa."""
    from xmclaw.core.persona.loader import PERSONA_BASENAMES
    assert set(AUTO_SECTIONS.keys()) == set(PERSONA_BASENAMES)
