"""B-93: file_index.py — frontmatter parsing + memory dir scan.

Pins:
  * frontmatter at top → fields extracted, body stripped
  * file w/o frontmatter → fallback description from first 200 chars
  * tags as list AND comma-string both parse
  * journal/ subdirs are excluded
  * unreadable / non-md files skipped silently
  * render_manifest respects max_chars budget
"""
from __future__ import annotations

from pathlib import Path


from xmclaw.providers.memory.file_index import (
    MemoryFileEntry,
    parse_frontmatter,
    render_manifest,
    scan_memory_files,
)


# ── parse_frontmatter ──────────────────────────────────────────────────


def test_parse_frontmatter_extracts_description_and_tags() -> None:
    text = (
        "---\n"
        "description: how Anthropic stream events work\n"
        "tags: [llm, streaming]\n"
        "---\n"
        "\n"
        "Body of the note here.\n"
    )
    fields, body = parse_frontmatter(text)
    assert fields["description"] == "how Anthropic stream events work"
    assert fields["tags"] == ["llm", "streaming"]
    # ``\s*`` greedy-matches both newlines after the closing ``---``,
    # so body starts at the first non-whitespace char.
    assert body.lstrip().startswith("Body of the note here.")


def test_parse_frontmatter_no_header_returns_empty() -> None:
    text = "Just markdown body with no header.\n"
    fields, body = parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_parse_frontmatter_quoted_values() -> None:
    text = (
        "---\n"
        'description: "with: colons inside"\n'
        "---\n"
        "body\n"
    )
    fields, _ = parse_frontmatter(text)
    assert fields["description"] == "with: colons inside"


def test_parse_frontmatter_inline_csv_tags() -> None:
    text = (
        "---\n"
        "description: x\n"
        "tags: build, frontend\n"
        "---\n"
        "body\n"
    )
    fields, _ = parse_frontmatter(text)
    # Bracket-less form falls back to plain string; scan_memory_files
    # then splits on commas. Verified separately below.
    assert fields["tags"] == "build, frontend"


# ── scan_memory_files ──────────────────────────────────────────────────


def test_scan_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    assert scan_memory_files(tmp_path / "nope") == []


def test_scan_picks_up_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "build.md").write_text(
        "---\n"
        "description: Build pipeline notes\n"
        "tags: [build, deps]\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )
    entries = scan_memory_files(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "build"
    assert e.description == "Build pipeline notes"
    assert e.tags == ("build", "deps")


def test_scan_falls_back_to_first_lines_when_no_frontmatter(
    tmp_path: Path,
) -> None:
    (tmp_path / "raw.md").write_text(
        "# Some Heading\n\nFirst real sentence here.\nSecond line.\n",
        encoding="utf-8",
    )
    entries = scan_memory_files(tmp_path)
    assert len(entries) == 1
    # The leading "# Some Heading" gets used as the description (after
    # the # is stripped) — that's our fallback behaviour.
    assert "Some Heading" in entries[0].description


def test_scan_excludes_journal_subdir(tmp_path: Path) -> None:
    (tmp_path / "topnote.md").write_text("a\n", encoding="utf-8")
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / "2026-04-29.md").write_text("daily\n", encoding="utf-8")
    entries = scan_memory_files(tmp_path)
    assert {e.name for e in entries} == {"topnote"}


def test_scan_csv_tag_format(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        "---\ndescription: x\ntags: a, b, c\n---\nbody\n",
        encoding="utf-8",
    )
    entries = scan_memory_files(tmp_path)
    assert entries[0].tags == ("a", "b", "c")


# ── render_manifest ─────────────────────────────────────────────────────


def test_render_manifest_basic() -> None:
    entries = [
        MemoryFileEntry(
            path=Path("/a.md"), name="alpha",
            description="first note", tags=("t1",),
        ),
        MemoryFileEntry(
            path=Path("/b.md"), name="beta",
            description="second", tags=(),
        ),
    ]
    out = render_manifest(entries)
    assert "alpha: first note" in out
    assert "[t1]" in out
    assert "beta: second" in out


def test_render_manifest_truncates_when_over_budget() -> None:
    # 50 entries with long descriptions
    entries = [
        MemoryFileEntry(
            path=Path(f"/n{i}.md"), name=f"n{i}",
            description="x" * 150,
        )
        for i in range(50)
    ]
    out = render_manifest(entries, max_chars=2000)
    assert "more files truncated" in out
    # Should be at or below cap (with one extra line for truncation note).
    assert len(out) < 3000
