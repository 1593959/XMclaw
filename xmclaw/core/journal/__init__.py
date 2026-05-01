"""Per-session journal — Epic #24 Phase 2.1.

Subscribes to the EventBus, buffers events per session, and on
``SESSION_LIFECYCLE phase=destroy`` flushes a single
:class:`JournalEntry` (mechanical session metadata: turn count, tool
calls, grader summary, anti-req violations) to
``~/.xmclaw/v2/journal/<YYYY-MM>/<session_id>.jsonl``.

Phase 2.1 stops at *mechanical* metadata. Phase 2.2 will layer LLM
reflection text on top (an extra `reflection` field), driven by the
same writer subscribing to the same destroy signal but invoking the
LLM after the mechanical row is already on disk so a missing /
slow LLM never blocks the journal.

Path policy (anti-req from 2026-05-01 user feedback): write path ==
read path. JournalReader reads exactly the same files JournalWriter
wrote. No mirror copies, no shadow indexes — the JSONL **is** the
journal.
"""
from __future__ import annotations

from xmclaw.core.journal.models import JournalEntry, ToolCallSummary
from xmclaw.core.journal.journal import JournalReader, JournalWriter

__all__ = [
    "JournalEntry",
    "JournalReader",
    "JournalWriter",
    "ToolCallSummary",
]
