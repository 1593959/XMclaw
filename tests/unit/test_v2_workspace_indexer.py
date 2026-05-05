"""B-210: workspace code indexing — chunk_code + denylist + classify.

Pre-B-210: MemoryFileIndexer only walked persona/journal markdown.
Asking "how does run_turn work?" forced the agent to file_read whole
files; vector recall couldn't help.

B-210 extends the indexer to optionally walk workspace roots and
chunk source files via sliding window. New chunks land in the same
sqlite-vec table tagged ``kind='code_chunk'`` (vs the existing
``file_chunk`` for persona/journal). memory_ctx auto-injection
skips both kinds; memory_search lets the agent explicitly query
``code_chunk`` for code recall.

These tests pin:
* chunk_code sliding-window behaviour (overlap, blank-line snap)
* the denylist + extension allowlist filter
* path classification (persona ⇒ file_chunk, workspace ⇒ code_chunk)
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.daemon.memory_indexer import (
    _CODE_DIR_DENYLIST,
    _CODE_FILE_EXTENSIONS,
    _CODE_FILE_MAX_BYTES,
    _is_code_path_allowed,
    _iter_workspace_files,
)
from xmclaw.utils.text_chunk import chunk_code


# ── chunk_code sliding window ────────────────────────────────────


def test_chunk_code_short_file_one_chunk() -> None:
    """File under max_lines threshold yields a single chunk."""
    text = "def hello():\n    return 'world'\n"
    out = chunk_code(text, max_lines=200)
    assert len(out) == 1
    assert out[0].start_line == 1
    assert out[0].end_line == 2
    assert "def hello" in out[0].text


def test_chunk_code_empty_yields_empty() -> None:
    assert chunk_code("") == []
    assert chunk_code("   \n  \n") == []


def test_chunk_code_long_file_overlapping_windows() -> None:
    """A 500-line file with max=200 + overlap=30 yields 3 chunks
    that overlap by ~30 lines each."""
    lines = [f"line_{i}" for i in range(500)]
    text = "\n".join(lines)
    out = chunk_code(text, max_lines=200, overlap_lines=30)
    assert len(out) >= 3
    # Each chunk must be reasonably sized.
    for c in out:
        assert (c.end_line - c.start_line + 1) <= 220  # max+slack
    # Adjacent chunks should overlap.
    for prev, nxt in zip(out, out[1:]):
        # Next chunk starts BEFORE prev ends ⇒ overlap exists.
        assert nxt.start_line <= prev.end_line


def test_chunk_code_snaps_to_blank_line_boundary() -> None:
    """When a blank line is near the target boundary, the chunker
    should land on it — keeps function bodies intact."""
    parts = []
    # First 'function': lines 1-50
    parts.extend([f"# fn1 line {i}" for i in range(50)])
    # Blank line at 51 (the natural break)
    parts.append("")
    # Second 'function': lines 52-150
    parts.extend([f"# fn2 line {i}" for i in range(99)])
    text = "\n".join(parts)
    out = chunk_code(text, max_lines=50, overlap_lines=5)
    # First chunk should end at the blank line (51) or right before it.
    assert out[0].end_line in (50, 51)


# ── code path filter ─────────────────────────────────────────────


def test_denylist_blocks_node_modules(tmp_path: Path) -> None:
    """``node_modules`` is the canonical "vendored garbage" — must
    never make it into the vector store. Same for build outputs."""
    nm = tmp_path / "node_modules" / "lodash" / "index.js"
    nm.parent.mkdir(parents=True)
    nm.write_text("module.exports = {}", encoding="utf-8")
    assert not _is_code_path_allowed(nm)


def test_denylist_blocks_pycache_and_venv(tmp_path: Path) -> None:
    cases = [
        tmp_path / "__pycache__" / "x.pyc",
        tmp_path / ".venv" / "lib" / "site-packages" / "foo.py",
        tmp_path / ".git" / "HEAD",
        tmp_path / "dist" / "bundle.js",
    ]
    for p in cases:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        assert not _is_code_path_allowed(p), f"should be denied: {p}"


def test_extension_allowlist_blocks_binaries(tmp_path: Path) -> None:
    """A file at a clean path but with .bin / .exe / .png extension
    must be filtered — vector indexing binaries is wasted bytes."""
    cases = [
        tmp_path / "screenshot.png",
        tmp_path / "tool.exe",
        tmp_path / "archive.zip",
        tmp_path / "data.bin",
    ]
    for p in cases:
        p.write_bytes(b"\x00\x01\x02\x03")
        assert not _is_code_path_allowed(p)


def test_extension_allowlist_passes_common_source(tmp_path: Path) -> None:
    """The whitelist must cover at minimum: py, js, ts, md, rs, go,
    rb, java, html, json, toml, yaml, sql."""
    # Sample one extension from each major language family we wired.
    samples = [
        ("foo.py", b"print('hi')"),
        ("foo.js", b"console.log(1)"),
        ("foo.ts", b"const x: number = 1;"),
        ("README.md", b"# title"),
        ("foo.rs", b"fn main() {}"),
        ("foo.go", b"package main"),
        ("foo.rb", b"puts 'hi'"),
        ("Foo.java", b"class Foo {}"),
        ("page.html", b"<p>hi</p>"),
        ("config.toml", b"[s]\nk='v'"),
        ("data.json", b'{"k":1}'),
        ("query.sql", b"SELECT 1"),
    ]
    for name, content in samples:
        p = tmp_path / name
        p.write_bytes(content)
        assert _is_code_path_allowed(p), f"should be allowed: {p}"


def test_size_cap_blocks_huge_files(tmp_path: Path) -> None:
    """A minified 1MB JS bundle should NOT be embedded — too noisy."""
    p = tmp_path / "huge.js"
    p.write_bytes(b"x" * (_CODE_FILE_MAX_BYTES + 1))
    assert not _is_code_path_allowed(p)


def test_iter_workspace_files_walks_recursively(tmp_path: Path) -> None:
    """``_iter_workspace_files`` must descend into subdirectories
    while skipping denylisted ones."""
    # Build a small tree:
    #   root/
    #     keep.py        ← yielded
    #     sub/keep.py    ← yielded
    #     node_modules/no.js   ← skipped (denylist)
    #     huge.js        ← skipped (size)
    #     data.bin       ← skipped (extension)
    (tmp_path / "keep.py").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "keep.py").write_text("b", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "no.js").write_text("c", encoding="utf-8")
    (tmp_path / "huge.js").write_bytes(b"x" * (_CODE_FILE_MAX_BYTES + 1))
    (tmp_path / "data.bin").write_bytes(b"\x00")

    found = list(_iter_workspace_files([tmp_path]))
    found_names = sorted(p.name for p in found)
    assert found_names == ["keep.py", "keep.py"]  # both keep.py files


def test_iter_workspace_files_handles_missing_root(tmp_path: Path) -> None:
    """A non-existent root is silently skipped — no crash."""
    bogus = tmp_path / "does_not_exist"
    found = list(_iter_workspace_files([bogus]))
    assert found == []


# ── invariants ───────────────────────────────────────────────────


def test_denylist_contains_critical_dirs() -> None:
    """Pin the denylist so a refactor doesn't accidentally drop
    .git / node_modules / __pycache__ — those are the load-bearing
    entries."""
    must_have = {
        ".git", "node_modules", "__pycache__", ".venv",
        "dist", "build",
    }
    assert must_have <= _CODE_DIR_DENYLIST


def test_allowlist_covers_xmclaw_self_indexing() -> None:
    """XMclaw is written in Python with a markdown README and
    JSON config. The allowlist MUST include .py + .md + .json
    so the agent can self-recall its own source."""
    must_have = {".py", ".md", ".json"}
    assert must_have <= _CODE_FILE_EXTENSIONS
