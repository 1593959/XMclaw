"""User profile extraction — Epic #24 Phase 2.2.

Subscribes to USER_MESSAGE / LLM_RESPONSE / SESSION_LIFECYCLE, buffers
recent turns per session, and on every Nth turn (or session destroy)
calls a user-supplied LLM extractor to identify *durable preference
deltas* (e.g. "user prefers terse markdown answers", "user runs
Windows 11"). Confirmed deltas are appended to the active persona's
``USER.md`` via the standard ``atomic_write_text`` path.

Path policy (anti-req from 2026-05-01): the extractor writes to
**exactly the same** ``USER.md`` that the persona assembler reads on
every turn — no shadow store, no parallel index. After a flush a
``USER_PROFILE_UPDATED`` event invalidates the system-prompt cache so
the next turn picks up the new lines.

Phase 2.2 ships the *extractor harness*: subscription + buffering +
threshold + flush, with a pluggable callable for the actual LLM
extraction step. The default callable is a no-op (returns ``[]``);
real LLM-driven extraction is wired up in Phase 2.4 inside the daemon
factory once an LLM provider is available.
"""
from __future__ import annotations

from xmclaw.core.profile.extractor import ProfileExtractor, noop_extractor
from xmclaw.core.profile.models import ProfileDelta

__all__ = [
    "ProfileDelta",
    "ProfileExtractor",
    "noop_extractor",
]
