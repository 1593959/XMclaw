"""B-93: relevant_picker.py — LLM picks top-K memory files.

Pins:
  * happy path: LLM returns valid JSON, picker maps filenames to
    matching entries in order
  * malformed LLM response → empty list (no crash)
  * picker hallucinated filenames → those drop out, valid ones stay
  * empty entries / empty query → empty list (no LLM call needed)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from xmclaw.providers.memory.file_index import MemoryFileEntry
from xmclaw.providers.memory.relevant_picker import (
    _parse_pick_response,
    find_relevant_memories,
)


def _entry(name: str, desc: str = "") -> MemoryFileEntry:
    return MemoryFileEntry(
        path=Path(f"/{name}.md"), name=name, description=desc or name,
    )


class _StubLLM:
    """Minimal LLM stub: returns a pre-canned content string."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[Any] = []

    async def complete(self, messages, tools=None):
        self.calls.append(messages)

        class _R:
            def __init__(self, c: str) -> None:
                self.content = c

        return _R(self._content)


# ── _parse_pick_response ───────────────────────────────────────────────


def test_parse_strict_json_object() -> None:
    raw = '{"files": ["a", "b"]}'
    assert _parse_pick_response(raw) == ["a", "b"]


def test_parse_strict_json_array() -> None:
    raw = '["a", "b"]'
    assert _parse_pick_response(raw) == ["a", "b"]


def test_parse_lenient_extracts_array_from_prose() -> None:
    raw = 'Here you go: ["alpha", "beta"] — that should help.'
    assert _parse_pick_response(raw) == ["alpha", "beta"]


def test_parse_garbage_returns_empty() -> None:
    assert _parse_pick_response("nothing here") == []
    assert _parse_pick_response("") == []


# ── find_relevant_memories ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_returns_picked_entries() -> None:
    entries = [_entry("alpha"), _entry("beta"), _entry("gamma")]
    llm = _StubLLM('{"files": ["beta", "gamma"]}')
    out = await find_relevant_memories(
        query="something", entries=entries, llm=llm, k=5,
    )
    assert [e.name for e in out] == ["beta", "gamma"]
    # LLM was actually called.
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_picker_drops_hallucinated_filenames() -> None:
    entries = [_entry("alpha"), _entry("beta")]
    # Picker invents "gamma" — skip it, keep "alpha".
    llm = _StubLLM('{"files": ["alpha", "gamma"]}')
    out = await find_relevant_memories(
        query="x", entries=entries, llm=llm, k=5,
    )
    assert [e.name for e in out] == ["alpha"]


@pytest.mark.asyncio
async def test_picker_strips_md_extension() -> None:
    entries = [_entry("alpha")]
    llm = _StubLLM('{"files": ["alpha.md"]}')
    out = await find_relevant_memories(
        query="x", entries=entries, llm=llm, k=5,
    )
    assert [e.name for e in out] == ["alpha"]


@pytest.mark.asyncio
async def test_empty_entries_skips_llm_call() -> None:
    llm = _StubLLM('{"files": ["whatever"]}')
    out = await find_relevant_memories(
        query="x", entries=[], llm=llm, k=5,
    )
    assert out == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_empty_query_skips_llm_call() -> None:
    entries = [_entry("alpha")]
    llm = _StubLLM('{"files": ["alpha"]}')
    out = await find_relevant_memories(
        query="   ", entries=entries, llm=llm, k=5,
    )
    assert out == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_llm_failure_returns_empty() -> None:
    class _BoomLLM:
        async def complete(self, messages, tools=None):
            raise RuntimeError("network down")

    out = await find_relevant_memories(
        query="x", entries=[_entry("a")], llm=_BoomLLM(), k=5,
    )
    assert out == []


@pytest.mark.asyncio
async def test_picker_respects_k_cap() -> None:
    entries = [_entry(f"n{i}") for i in range(10)]
    llm = _StubLLM(
        '{"files": ["n0","n1","n2","n3","n4","n5","n6"]}'
    )
    out = await find_relevant_memories(
        query="x", entries=entries, llm=llm, k=3,
    )
    assert [e.name for e in out] == ["n0", "n1", "n2"]
