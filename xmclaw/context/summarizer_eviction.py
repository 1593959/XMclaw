"""Context-aware eviction planning for summarizer-driven compaction.

This module is intentionally policy-only: it does not call an LLM, mutate
messages, or delete anything. The compressor and future summarizer agents can
use the returned plan as an auditable contract for what will be summarized,
what remains verbatim, and why.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from xmclaw.providers.llm.base import Message


@dataclass(frozen=True, slots=True)
class EvictionRange:
    """Half-open message range selected for summarization/eviction."""

    start: int
    end: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SummarizerEvictionPlan:
    """Serializable plan for summarizer-backed message eviction."""

    session_id: str
    messages_before: int
    summarize_start: int
    summarize_end: int
    source_indices: tuple[int, ...]
    summarize_indices: tuple[int, ...]
    preserved_indices: tuple[int, ...]
    protected_indices: tuple[int, ...]
    ranges: tuple[EvictionRange, ...] = ()
    evict_ratio: float = 0.0
    summary_kind: str = "conversation_middle"
    summary_provenance: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def should_summarize(self) -> bool:
        return bool(self.summarize_indices)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "messages_before": self.messages_before,
            "summarize_start": self.summarize_start,
            "summarize_end": self.summarize_end,
            "source_indices": list(self.source_indices),
            "summarize_indices": list(self.summarize_indices),
            "preserved_indices": list(self.preserved_indices),
            "protected_indices": list(self.protected_indices),
            "ranges": [r.to_dict() for r in self.ranges],
            "evict_ratio": self.evict_ratio,
            "summary_kind": self.summary_kind,
            "summary_provenance": dict(self.summary_provenance),
            "provenance": dict(self.provenance),
        }


class SummarizerEvictionPlanner:
    """Build a conservative compaction plan from known context boundaries."""

    def __init__(
        self,
        *,
        preserve_user_messages: bool = True,
        protect_latest_user: bool = True,
        min_source_messages: int = 1,
    ) -> None:
        self.preserve_user_messages = preserve_user_messages
        self.protect_latest_user = protect_latest_user
        self.min_source_messages = max(1, int(min_source_messages))

    def plan(
        self,
        messages: list[Message],
        *,
        session_id: str = "",
        summarize_start: int,
        summarize_end: int,
        reason: str = "middle_context_over_budget",
        focus_topic: str | None = None,
        model_profile: str | None = None,
        created_at: float | None = None,
    ) -> SummarizerEvictionPlan:
        n = len(messages)
        start = max(0, min(int(summarize_start), n))
        end = max(start, min(int(summarize_end), n))
        start = self._align_start(messages, start, end)
        end = self._align_end(messages, start, end)

        source = tuple(range(start, end))
        latest_user = self._latest_user_index(messages)
        head_tail_protected = set(range(0, start)) | set(range(end, n))
        if self.protect_latest_user and latest_user is not None:
            head_tail_protected.add(latest_user)

        preserved = set(head_tail_protected)
        summarize: list[int] = []
        for idx in source:
            msg = messages[idx]
            if self.preserve_user_messages and msg.role == "user":
                preserved.add(idx)
                continue
            if self.protect_latest_user and idx == latest_user:
                preserved.add(idx)
                continue
            summarize.append(idx)

        if len(source) < self.min_source_messages:
            summarize = []

        ranges = self._ranges_from_indices(summarize, reason)
        ratio = (len(summarize) / n) if n else 0.0
        summary_kind = "conversation_middle"
        ts = time.time() if created_at is None else float(created_at)
        summary_provenance: dict[str, Any] = {
            "source": "SummarizerEvictionPlanner",
            "session_id": session_id,
            "summary_kind": summary_kind,
            "source_message_range": [start, end],
            "source_indices": list(source),
            "summarize_indices": list(summarize),
            "preserved_indices": sorted(preserved),
            "evict_ratio": ratio,
            "model_profile": model_profile or "",
            "created_at": ts,
        }
        if focus_topic:
            summary_provenance["focus_topic"] = focus_topic
        provenance: dict[str, Any] = {
            "source": "SummarizerEvictionPlanner",
            "reason": reason,
            "range": [start, end],
            "preserve_user_messages": self.preserve_user_messages,
            "protect_latest_user": self.protect_latest_user,
            "summary_provenance": summary_provenance,
        }
        if focus_topic:
            provenance["focus_topic"] = focus_topic

        return SummarizerEvictionPlan(
            session_id=session_id,
            messages_before=n,
            summarize_start=start,
            summarize_end=end,
            source_indices=source,
            summarize_indices=tuple(summarize),
            preserved_indices=tuple(sorted(preserved)),
            protected_indices=tuple(sorted(head_tail_protected)),
            ranges=tuple(ranges),
            evict_ratio=ratio,
            summary_kind=summary_kind,
            summary_provenance=summary_provenance,
            provenance=provenance,
        )

    @staticmethod
    def _align_start(messages: list[Message], start: int, end: int) -> int:
        while start < end and messages[start].role == "tool":
            start += 1
        return start

    @staticmethod
    def _align_end(messages: list[Message], start: int, end: int) -> int:
        if end <= start or end >= len(messages):
            return end
        prev = messages[end - 1]
        current = messages[end]
        if prev.role == "assistant" and prev.tool_calls and current.role == "tool":
            tool_ids = {
                getattr(tc, "id", "") for tc in prev.tool_calls or ()
                if getattr(tc, "id", "")
            }
            if getattr(current, "tool_call_id", None) in tool_ids:
                return min(len(messages), end + 1)
        return end

    @staticmethod
    def _latest_user_index(messages: list[Message]) -> int | None:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].role == "user":
                return idx
        return None

    @staticmethod
    def _ranges_from_indices(indices: list[int], reason: str) -> list[EvictionRange]:
        if not indices:
            return []
        ranges: list[EvictionRange] = []
        start = indices[0]
        prev = indices[0]
        for idx in indices[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            ranges.append(EvictionRange(start=start, end=prev + 1, reason=reason))
            start = idx
            prev = idx
        ranges.append(EvictionRange(start=start, end=prev + 1, reason=reason))
        return ranges


__all__ = [
    "EvictionRange",
    "SummarizerEvictionPlan",
    "SummarizerEvictionPlanner",
]
