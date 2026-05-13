"""Skill pattern detector — Wave 19.

Scans the event bus for repeated sequences of tool calls and proposes
each repeated sequence as a candidate skill. Used both by:

  * ``SkillDraftSuggester`` — a ProactiveTrigger that runs weekly,
    emits a PROACTIVE_PROPOSAL ("我注意到你最近多次 X → Y → Z，要不要
    打包成 skill？")
  * Future: an /api/v2/skills/draft-suggestions endpoint surfacing the
    patterns on the Evolution page

Algorithm:

  1. Pull all TOOL_CALL_EMITTED events in the lookback window
  2. Group by session_id; each session becomes an ordered list of
     tool names: ["screen_capture", "image_read", "todo_write", ...]
  3. Extract n-grams of length 2..max_len from each session
  4. Count n-grams that appear in ≥ min_distinct_sessions different
     sessions (cross-session counting kills "this user clicked the
     same button 50 times in one debug session" noise)
  5. Return Pattern objects sorted by impact (total_occurrences desc)

The detector itself is pure analysis; it doesn't emit events or
proposals. Caller decides what to do with the patterns.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# Tools we don't want to highlight in a skill proposal because they're
# either universal (every turn uses them) or stateful in ways that
# don't translate well to standalone skills.
_EXCLUDED_TOOLS = frozenset({
    "todo_write",
    "todo_read",
    "remember",
    "learn_about_user",
    "recall_user_preferences",
    "agent_invoke",
})


@dataclass(frozen=True, slots=True)
class Pattern:
    """A detected tool-call sequence that recurs across sessions."""
    tool_sequence: tuple[str, ...]
    distinct_sessions: int
    total_occurrences: int
    sample_session_ids: tuple[str, ...] = field(default_factory=tuple)


def analyze_patterns(
    *,
    bus: Any,
    lookback_days: float = 7.0,
    min_distinct_sessions: int = 3,
    min_length: int = 2,
    max_length: int = 4,
    max_results: int = 10,
) -> list[Pattern]:
    """Scan the event log and return repeated tool-call sequences.

    Args:
        bus: object with ``.query(since, types, limit)`` (SqliteEventBus
             or test fake).
        lookback_days: how far back to scan.
        min_distinct_sessions: pattern must show up in this many
             distinct sessions to count (filters single-session loops).
        min_length / max_length: n-gram length window.
        max_results: cap output rows.

    Returns:
        list of Pattern sorted by total_occurrences desc.
    """
    query = getattr(bus, "query", None) if bus is not None else None
    if not callable(query):
        return []

    import time as _time
    since = _time.time() - max(1.0, lookback_days) * 86400.0
    try:
        events = query(
            since=since,
            types=["tool_call_emitted"],
            limit=20_000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "skill_pattern_detector.query_failed err=%s", exc,
        )
        return []

    # session_id → ordered list of tool names
    per_session: dict[str, list[str]] = defaultdict(list)
    for e in events:
        payload = getattr(e, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in _EXCLUDED_TOOLS:
            continue
        sid = getattr(e, "session_id", None) or "unknown"
        per_session[sid].append(name)

    # ngram → set of session_ids seen + total count
    ngram_sessions: dict[tuple[str, ...], set[str]] = defaultdict(set)
    ngram_total: dict[tuple[str, ...], int] = defaultdict(int)
    for sid, seq in per_session.items():
        for n in range(min_length, max_length + 1):
            if len(seq) < n:
                continue
            for i in range(len(seq) - n + 1):
                ng = tuple(seq[i : i + n])
                # Drop degenerate runs of the same tool (same name
                # repeated): "screen_capture, screen_capture" is just
                # one tool being retried, not a workflow.
                if len(set(ng)) == 1:
                    continue
                ngram_sessions[ng].add(sid)
                ngram_total[ng] += 1

    # Filter + collapse: a longer ngram that contains a shorter one
    # should win when both clear the threshold (the longer is more
    # specific). We pick the longest qualified suffix-free representative.
    qualified: list[Pattern] = []
    for ng, sids in ngram_sessions.items():
        if len(sids) < min_distinct_sessions:
            continue
        qualified.append(Pattern(
            tool_sequence=ng,
            distinct_sessions=len(sids),
            total_occurrences=ngram_total[ng],
            sample_session_ids=tuple(sorted(sids)[:3]),
        ))

    # Drop any pattern that's a strict subsequence of another
    # qualified pattern with the same or greater distinct_sessions
    # count — keeps the recommendations from drowning in shorter
    # prefixes.
    qualified.sort(
        key=lambda p: (
            -p.total_occurrences,
            -p.distinct_sessions,
            -len(p.tool_sequence),
        ),
    )
    deduped: list[Pattern] = []
    for cand in qualified:
        if _is_subsequence_of_any(cand, deduped):
            continue
        deduped.append(cand)
        if len(deduped) >= max_results:
            break
    return deduped


def _is_subsequence_of_any(
    cand: Pattern, existing: list[Pattern],
) -> bool:
    """Is ``cand.tool_sequence`` a contiguous subsequence of any
    already-accepted pattern with ≥ same session count?"""
    for ex in existing:
        if ex.distinct_sessions < cand.distinct_sessions:
            continue
        if len(ex.tool_sequence) <= len(cand.tool_sequence):
            continue
        # Contiguous substring check on tuple.
        ex_seq = ex.tool_sequence
        c_seq = cand.tool_sequence
        for i in range(len(ex_seq) - len(c_seq) + 1):
            if ex_seq[i : i + len(c_seq)] == c_seq:
                return True
    return False


def format_pattern_proposal(p: Pattern) -> str:
    """Produce a human-readable PROACTIVE_PROPOSAL message body."""
    seq = " → ".join(f"`{n}`" for n in p.tool_sequence)
    return (
        f"💡 我注意到你最近常做这个：\n\n  {seq}\n\n"
        f"在 {p.distinct_sessions} 个会话里出现了 {p.total_occurrences} 次。"
        f"要不要我帮你把它打包成一个 skill？"
    )


__all__ = [
    "Pattern",
    "analyze_patterns",
    "format_pattern_proposal",
]
