"""MagicDocs — auto-updated markdown files.

Wave-32+ (2026-05-18). Ports the free-code-main ``services/MagicDocs/``
pattern with an XMclaw-shaped trigger model.

How it works
============

1. **Detection.** When the agent reads a file, the file_read handler
   calls :func:`maybe_register` on the first few hundred bytes. If
   the file starts with a Magic Doc header

       # MAGIC DOC: <title>
       *<optional one-line instructions>*

   we register the absolute path in :data:`_TRACKED` (process-level
   set). Already-tracked paths are no-ops.

2. **Triggering.** After every turn completes, the WS handler calls
   :func:`schedule_updates`. For each tracked doc whose time-since-
   last-update exceeds :data:`UPDATE_COOLDOWN_S`, the function
   spawns a background sub-task that re-reads the doc, looks at the
   conversation history, and edits the doc to reflect any new
   learnings.

3. **Update gating.** The background task gets a prompt that names
   the doc path + the rules ("only edit THIS file, only via
   apply_patch / file_write, never delete the MAGIC DOC header").
   The task uses :class:`AgentInterTools.submit_background` so it
   runs as a regular agent turn, inheriting the daemon's normal
   permission / sandbox checks — no special tool-whitelist
   plumbing required.

State persistence
=================

Nothing is persisted. The tracked set + per-doc timestamps are
in-memory, so a daemon restart drops them — the next time the user
reads a Magic Doc the tracking re-establishes. This is intentional:
durability of "do I owe doc X an update?" isn't worth a sqlite
table.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# How long to wait between updates of the same doc. 5 minutes is a
# good compromise — long enough that the user isn't paying for an
# update on every tiny turn, short enough that the doc reflects
# recent learnings within a normal work session.
UPDATE_COOLDOWN_S: float = 300.0

# Inspect at most this many bytes of file content for the header.
# Magic Doc header is always at line 1 — no need to scan the body.
_HEADER_SCAN_BYTES = 512

# Free-code-parity pattern: matches "# MAGIC DOC: <title>" at the
# start of any line. ``re.IGNORECASE | re.MULTILINE`` so the user
# can use "magic doc" / "MAGIC DOC" / "Magic Doc" interchangeably.
_HEADER_RE = re.compile(
    r"^#\s*MAGIC\s+DOC:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE,
)
# The italics line on the row immediately after the header carries
# optional per-doc instructions ("Only summarize the public API",
# "Group entries by date", etc).
_ITALICS_RE = re.compile(r"^\s*[_*](.+?)[_*]\s*$", re.MULTILINE)


@dataclass(slots=True)
class _DocInfo:
    path: str
    title: str
    instructions: str | None = None
    last_update_at: float | None = None
    updates_attempted: int = 0


@dataclass
class _Registry:
    docs: dict[str, _DocInfo] = field(default_factory=dict)


_TRACKED = _Registry()


def detect_header(content: str) -> tuple[str, str | None] | None:
    """Return ``(title, instructions)`` if the content begins with a
    Magic Doc header; ``None`` otherwise. The italics line is the
    line IMMEDIATELY after the header (after at most one blank line).
    """
    if not content:
        return None
    head = content[:_HEADER_SCAN_BYTES]
    m = _HEADER_RE.search(head)
    if not m:
        return None
    title = m.group(1).strip()
    after = head[m.end():]
    # Allow at most one blank line, then look for an italics line.
    lines = after.split("\n", 3)
    for line in lines[:3]:
        if not line.strip():
            continue
        im = _ITALICS_RE.match(line)
        if im:
            return title, im.group(1).strip()
        # First non-blank line wasn't italics — no instructions.
        break
    return title, None


def maybe_register(file_path: str, content: str) -> bool:
    """Register ``file_path`` as a Magic Doc if its content has the
    header. Returns True if newly tracked (or already tracked + the
    title changed); False otherwise.

    Idempotent. Safe to call from the hot path of ``file_read``."""
    detected = detect_header(content)
    if detected is None:
        return False
    title, instructions = detected
    try:
        normalized = str(Path(file_path).resolve())
    except OSError:
        normalized = file_path
    info = _TRACKED.docs.get(normalized)
    if info is None:
        _TRACKED.docs[normalized] = _DocInfo(
            path=normalized, title=title, instructions=instructions,
        )
        return True
    if info.title != title or info.instructions != instructions:
        info.title = title
        info.instructions = instructions
        return True
    return False


def tracked_docs() -> list[_DocInfo]:
    """Snapshot of currently tracked Magic Docs. Returns copies so
    callers can iterate without worrying about concurrent mutation."""
    return [
        _DocInfo(
            path=d.path, title=d.title, instructions=d.instructions,
            last_update_at=d.last_update_at,
            updates_attempted=d.updates_attempted,
        )
        for d in _TRACKED.docs.values()
    ]


def forget(file_path: str) -> bool:
    """Remove a doc from tracking (called when the file is deleted
    or its header is removed). Returns True if it was tracked."""
    try:
        normalized = str(Path(file_path).resolve())
    except OSError:
        normalized = file_path
    return _TRACKED.docs.pop(normalized, None) is not None


def clear_all() -> None:
    """Test helper — wipe the registry between cases."""
    _TRACKED.docs.clear()


def _due_for_update(info: _DocInfo, now: float) -> bool:
    if info.last_update_at is None:
        return True
    return (now - info.last_update_at) >= UPDATE_COOLDOWN_S


def build_update_prompt(info: _DocInfo) -> str:
    """Construct the user-prompt the background updater sub-task
    receives. Names the doc path + spells out the contract so the
    task can't drift into editing unrelated files."""
    instr_line = (
        f"\n\nPer-doc instructions: {info.instructions}\n"
        if info.instructions else ""
    )
    return (
        f"You are updating a Magic Doc.\n\n"
        f"  • Title: {info.title}\n"
        f"  • Path: {info.path}\n"
        f"{instr_line}\n"
        "Read the current file content with file_read, look at the "
        "last ~20 messages of THIS session's history (mentally — they "
        "are in your context), and decide whether the doc needs an "
        "update to reflect new learnings, decisions, or code "
        "structures introduced since the last update.\n\n"
        "Rules:\n"
        "  • ONLY edit this exact file. Do NOT touch any other file.\n"
        "  • Use apply_patch or file_write. Preserve the "
        "    `# MAGIC DOC:` header verbatim — removing it un-tracks "
        "    the doc.\n"
        "  • If nothing meaningful has changed, output a one-line "
        "    confirmation and STOP — do not edit. Spurious diffs "
        "    cost the user attention.\n"
        "  • Keep the doc concise. Magic Docs are summaries, not "
        "    encyclopedias.\n\n"
        "When done, your final assistant message should be either "
        "the diff you applied or `no update needed`."
    )


async def schedule_updates(agent_inter: Any | None) -> int:
    """Fire background update tasks for every tracked doc whose
    cooldown has expired. Returns the number of tasks dispatched.

    ``agent_inter`` is duck-typed for ``submit_background(agent_id,
    content, source=...)``. Pass ``None`` (or omit the wiring) to
    silently no-op — Magic Docs gracefully degrade when no
    delegation surface is available.
    """
    if agent_inter is None:
        return 0
    submit = getattr(agent_inter, "submit_background", None)
    if submit is None:
        return 0
    now = time.time()
    dispatched = 0
    for info in list(_TRACKED.docs.values()):
        if not _due_for_update(info, now):
            continue
        try:
            await submit(
                "main",
                build_update_prompt(info),
                source="magic_docs",
            )
        except Exception as exc:  # noqa: BLE001 — never break the user's turn
            logger.warning(
                "magic_docs.submit_failed path=%s err=%s", info.path, exc,
            )
            continue
        info.last_update_at = now
        info.updates_attempted += 1
        dispatched += 1
    return dispatched


__all__ = [
    "UPDATE_COOLDOWN_S",
    "build_update_prompt",
    "clear_all",
    "detect_header",
    "forget",
    "maybe_register",
    "schedule_updates",
    "tracked_docs",
]
