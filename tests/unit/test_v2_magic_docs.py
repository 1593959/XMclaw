"""MagicDocs — Wave-32+ (2026-05-18)."""
from __future__ import annotations

import time

import pytest

from xmclaw.cognition import magic_docs
from xmclaw.cognition.magic_docs import (
    UPDATE_COOLDOWN_S,
    build_update_prompt,
    clear_all,
    detect_header,
    forget,
    maybe_register,
    schedule_updates,
    tracked_docs,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_all()
    yield
    clear_all()


# ── detect_header ────────────────────────────────────────────────────────


def test_detect_header_basic() -> None:
    assert detect_header("# MAGIC DOC: api guide") == ("api guide", None)


def test_detect_header_with_instructions() -> None:
    text = "# MAGIC DOC: api guide\n*group by endpoint*\n\nbody"
    assert detect_header(text) == ("api guide", "group by endpoint")


def test_detect_header_with_blank_line_before_italics() -> None:
    """Free-code allows ONE blank line between header and italics."""
    text = "# MAGIC DOC: schema\n\n*one entry per table*\n"
    assert detect_header(text) == ("schema", "one entry per table")


def test_detect_header_case_insensitive() -> None:
    assert detect_header("# magic doc: lowered")[0] == "lowered"
    assert detect_header("# Magic Doc: titlecase")[0] == "titlecase"


def test_detect_header_returns_none_when_missing() -> None:
    assert detect_header("# just a regular heading") is None
    assert detect_header("") is None
    assert detect_header("body without header") is None


def test_detect_header_ignores_body_occurrences() -> None:
    """Header must be in the first 512 bytes — a mention deep in
    the body shouldn't trigger registration."""
    text = "# Other Heading\n\n" + ("filler line\n" * 200) + "# MAGIC DOC: too late"
    assert detect_header(text) is None


# ── maybe_register / forget ──────────────────────────────────────────────


def test_maybe_register_returns_true_first_time(tmp_path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("# MAGIC DOC: doc one\n")
    assert maybe_register(str(p), p.read_text()) is True
    assert len(tracked_docs()) == 1
    # Second register of the same path is a no-op.
    assert maybe_register(str(p), p.read_text()) is False
    assert len(tracked_docs()) == 1


def test_maybe_register_returns_true_when_title_changes(tmp_path) -> None:
    p = tmp_path / "doc.md"
    assert maybe_register(str(p), "# MAGIC DOC: original") is True
    # Same path, different title content → re-registers (returns True).
    assert maybe_register(str(p), "# MAGIC DOC: renamed") is True
    docs = tracked_docs()
    assert len(docs) == 1
    assert docs[0].title == "renamed"


def test_maybe_register_returns_false_for_non_magic_doc(tmp_path) -> None:
    p = tmp_path / "regular.md"
    assert maybe_register(str(p), "# normal heading") is False
    assert tracked_docs() == []


def test_forget_removes_tracked_entry(tmp_path) -> None:
    p = tmp_path / "doc.md"
    maybe_register(str(p), "# MAGIC DOC: x")
    assert forget(str(p)) is True
    assert tracked_docs() == []
    # Idempotent.
    assert forget(str(p)) is False


# ── build_update_prompt ──────────────────────────────────────────────────


def test_update_prompt_names_path_and_title(tmp_path) -> None:
    p = tmp_path / "doc.md"
    maybe_register(str(p), "# MAGIC DOC: api guide\n*group by endpoint*")
    info = tracked_docs()[0]
    prompt = build_update_prompt(info)
    assert "api guide" in prompt
    assert info.path in prompt
    assert "group by endpoint" in prompt
    # Hard rules must be present so the sub-task doesn't drift.
    assert "ONLY edit this exact file" in prompt
    assert "MAGIC DOC" in prompt


# ── schedule_updates ─────────────────────────────────────────────────────


class _RecorderInter:
    """Stub for AgentInterTools.submit_background — records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def submit_background(self, agent_id, content, *, source="ai"):
        self.calls.append((agent_id, content, source))
        return f"tk-{len(self.calls)}"


@pytest.mark.asyncio
async def test_schedule_updates_dispatches_pending(tmp_path) -> None:
    p = tmp_path / "doc.md"
    maybe_register(str(p), "# MAGIC DOC: x")
    inter = _RecorderInter()
    n = await schedule_updates(inter)
    assert n == 1
    assert len(inter.calls) == 1
    _agent_id, prompt, source = inter.calls[0]
    assert _agent_id == "main"
    assert source == "magic_docs"
    assert "MAGIC DOC" in prompt


@pytest.mark.asyncio
async def test_schedule_updates_respects_cooldown(tmp_path) -> None:
    p = tmp_path / "doc.md"
    maybe_register(str(p), "# MAGIC DOC: y")
    inter = _RecorderInter()
    # First dispatch goes through.
    assert await schedule_updates(inter) == 1
    # Second dispatch immediately after is gated by cooldown.
    assert await schedule_updates(inter) == 0
    assert len(inter.calls) == 1


@pytest.mark.asyncio
async def test_schedule_updates_handles_no_agent_inter() -> None:
    """Daemon without agent_inter wired must NOT crash — just no-op."""
    maybe_register("/tmp/fake", "# MAGIC DOC: fake")
    assert await schedule_updates(None) == 0


@pytest.mark.asyncio
async def test_schedule_updates_survives_submit_failure(tmp_path) -> None:
    """A submit that raises must NOT mark the doc as updated (so
    the next attempt retries) and must NOT propagate the exception
    (the user's turn already finished)."""
    class _BrokenInter:
        async def submit_background(self, *a, **kw):
            raise RuntimeError("kaboom")

    p = tmp_path / "doc.md"
    maybe_register(str(p), "# MAGIC DOC: z")
    n = await schedule_updates(_BrokenInter())
    assert n == 0
    info = tracked_docs()[0]
    # last_update_at stays None so the next attempt is still "due"
    # rather than waiting out the full cooldown.
    assert info.last_update_at is None


@pytest.mark.asyncio
async def test_schedule_updates_dispatches_after_cooldown_elapses(tmp_path) -> None:
    """If cooldown has elapsed (simulated by manually rolling back
    last_update_at), schedule_updates fires again."""
    p = tmp_path / "doc.md"
    maybe_register(str(p), "# MAGIC DOC: a")
    inter = _RecorderInter()
    await schedule_updates(inter)
    # Roll back the timestamp past the cooldown.
    magic_docs._TRACKED.docs[next(iter(magic_docs._TRACKED.docs))].last_update_at = (
        time.time() - UPDATE_COOLDOWN_S - 1
    )
    assert await schedule_updates(inter) == 1
    assert len(inter.calls) == 2


# ── end-to-end via file_read ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_read_auto_registers_magic_doc(tmp_path) -> None:
    """The whole point of MagicDocs: reading a tracked file via the
    file_read tool should auto-register it. Pin the behaviour at
    the integration boundary."""
    from xmclaw.core.ir import ToolCall
    from xmclaw.providers.tool.builtin import BuiltinTools

    p = tmp_path / "magic.md"
    p.write_text("# MAGIC DOC: my doc\n\nbody\n")
    tools = BuiltinTools()
    call = ToolCall(
        name="file_read", args={"path": str(p)}, provenance="synthetic",
    )
    res = await tools.invoke(call)
    assert res.ok, res.error
    paths = [d.path for d in tracked_docs()]
    assert any(p.name in path for path in paths)
