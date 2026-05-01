"""Profile data model.

A :class:`ProfileDelta` is one extracted observation about the user
(preference / style / constraint / habit). Frozen + slots so the
event payload can transport them safely without subscribers mutating
each other's view.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProfileDelta:
    """One observation about the user.

    ``kind`` is a coarse category to keep the auto-generated USER.md
    section navigable. The four currently recognized values:

    * ``preference``  — what the user likes / wants
                        (e.g. "prefers Markdown over HTML output")
    * ``constraint``  — non-negotiable boundary
                        (e.g. "do not call ``rm -rf`` even if asked")
    * ``style``       — how the user communicates
                        (e.g. "terse, expects ≤3 line replies")
    * ``habit``       — what the user actually does, observed
                        (e.g. "always rebases before pushing")

    Extractors are free to use other strings; the renderer keeps
    unknown kinds in a generic bucket rather than dropping them.

    ``confidence`` ∈ [0.0, 1.0]. The default flush threshold drops
    deltas with confidence < 0.5 (configurable on the extractor) —
    enough to suppress LLM hallucinations without losing real signal.

    ``source_session_id`` + ``source_event_id`` form the audit trail
    the user / Phase 2.5 ``journal_recall`` tool can use to ask "why
    did the agent think this?". Both are required: silent
    auto-extracted deltas are not allowed.
    """

    kind: str
    text: str
    confidence: float
    source_session_id: str
    source_event_id: str
    ts: float

    def to_jsonable(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "confidence": self.confidence,
            "source_session_id": self.source_session_id,
            "source_event_id": self.source_event_id,
            "ts": self.ts,
        }

    def render_line(self) -> str:
        """Single-line USER.md representation.

        Format:

            - [auto · {kind} · conf={confidence:.2f} · session={session_id}] text

        The leading ``[auto · …]`` prefix marks the line as
        machine-extracted so a future ``learn_about_user`` call (or
        the user editing the file by hand) can still distinguish what
        was hand-curated vs auto-derived.
        """
        return (
            f"- [auto · {self.kind} · conf={self.confidence:.2f} · "
            f"session={self.source_session_id}] {self.text}"
        )
