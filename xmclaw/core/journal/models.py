"""Journal data model.

A :class:`JournalEntry` is one session's record. Frozen + slots so
subscribers / readers can treat it as immutable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCallSummary:
    """One tool call in a session — what was called + did it succeed.

    The full grader verdict + tool result lives in the event bus
    (``events.db``); this is the abbreviated copy that survives in the
    journal so the reader doesn't have to join across two stores.
    """

    name: str
    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One session's journal record.

    Phase 2.1 fields are mechanical only. Phase 2.2 adds:

    * ``reflection: str | None`` — LLM-generated free-text summary.
    * ``lessons: tuple[str, ...]`` — extracted ``"next time, …"``
      action items.

    Phase 3 adds:

    * ``skill_proposals: tuple[str, ...]`` — candidate skill_ids the
      ``SkillProposer`` thinks should be born from this session's
      pattern.
    """

    session_id: str
    agent_id: str
    ts_start: float
    ts_end: float
    duration_s: float
    turn_count: int           # USER_MESSAGE events seen
    tool_calls: tuple[ToolCallSummary, ...] = field(default_factory=tuple)
    grader_avg_score: float | None = None
    grader_play_count: int = 0
    grader_lowest: float | None = None
    grader_highest: float | None = None
    anti_req_violations: int = 0
    schema_version: int = 1

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
            "duration_s": self.duration_s,
            "turn_count": self.turn_count,
            "tool_calls": [
                {"name": t.name, "ok": t.ok, "error": t.error}
                for t in self.tool_calls
            ],
            "grader_avg_score": self.grader_avg_score,
            "grader_play_count": self.grader_play_count,
            "grader_lowest": self.grader_lowest,
            "grader_highest": self.grader_highest,
            "anti_req_violations": self.anti_req_violations,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "JournalEntry":
        tool_calls = tuple(
            ToolCallSummary(
                name=str(t.get("name", "")),
                ok=bool(t.get("ok", False)),
                error=t.get("error"),
            )
            for t in data.get("tool_calls", []) or []
        )
        return cls(
            session_id=str(data["session_id"]),
            agent_id=str(data.get("agent_id", "")),
            ts_start=float(data["ts_start"]),
            ts_end=float(data["ts_end"]),
            duration_s=float(data["duration_s"]),
            turn_count=int(data.get("turn_count", 0)),
            tool_calls=tool_calls,
            grader_avg_score=(
                float(data["grader_avg_score"])
                if data.get("grader_avg_score") is not None else None
            ),
            grader_play_count=int(data.get("grader_play_count", 0)),
            grader_lowest=(
                float(data["grader_lowest"])
                if data.get("grader_lowest") is not None else None
            ),
            grader_highest=(
                float(data["grader_highest"])
                if data.get("grader_highest") is not None else None
            ),
            anti_req_violations=int(data.get("anti_req_violations", 0)),
            schema_version=int(data.get("schema_version", 1)),
        )
