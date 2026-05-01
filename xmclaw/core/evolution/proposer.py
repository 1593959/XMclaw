"""SkillProposer — Epic #24 Phase 3.

Walk recent ``JournalEntry`` history, find tool-name patterns the
agent repeats across sessions, ask a user-supplied LLM extractor to
draft a ``ProposedSkill`` for each promising pattern, and surface
those candidates onto the bus as ``SKILL_CANDIDATE_PROPOSED`` events.

The proposer **never** writes to ``SkillRegistry`` directly. Per
plan rule #1 ("HonestGrader is the only scoring entry"), proposals
land as audit + bus events; turning a proposal into an actual skill
version is gated by the existing CLI / UI approval flow
(``xmclaw evolve approve``). This is also what plan rule #4
("everything goes through events, not back-channels") demands.

Pattern discovery (Phase 3 minimal): count tool names across the
last N journal entries, keep those that appear in ≥ ``min_pattern_count``
distinct sessions. Phase 4 can layer co-occurrence, sequence
matching, and grader-weighted scoring on top of the same shape.

The LLM extractor is pluggable. The default is a no-op (returns []),
because:

* core/ cannot import LLM providers (DAG rule).
* Phase 3.2 wires a real LLM-backed extractor from the daemon
  factory once the LLM provider is available.

Audit trail: every ProposedSkill carries `evidence` referencing the
session_ids the pattern was observed in, so anti-req #12 ("no
evidence, no promotion") is satisfiable when the proposal eventually
lands in SkillRegistry.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from xmclaw.core.journal import JournalEntry, JournalReader

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProposedSkill:
    """An LLM-drafted skill candidate from a tool-use pattern.

    ``skill_id`` follows the same dotted-namespace convention the
    SkillRegistry uses (``demo.read_and_summarize``). ``body`` is the
    proposed SKILL.md procedure body (Phase 3 stub: brief description
    of what the skill should do; Phase 4 layers structured tool
    invocation steps).

    ``confidence`` ∈ [0.0, 1.0]; the daemon-side wrapper drops below
    ``min_confidence`` before publishing.

    ``evidence`` MUST be non-empty. It points to the session_ids
    where the pattern was observed, satisfying anti-req #12 if the
    proposal is later approved into SkillRegistry.
    """

    skill_id: str
    title: str
    description: str
    body: str
    triggers: tuple[str, ...]
    confidence: float
    evidence: tuple[str, ...]
    source_pattern: str

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError(
                "ProposedSkill.evidence MUST be non-empty (anti-req #12)"
            )

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "description": self.description,
            "body": self.body,
            "triggers": list(self.triggers),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "source_pattern": self.source_pattern,
        }


@dataclass(frozen=True, slots=True)
class _Pattern:
    """Detected repeating pattern in journal data.

    Phase 3 minimal: pattern == "tool ``name`` was used in N
    sessions". Phase 4 generalises to (tool sequence, error->recover
    pairs, grader-improvement trends).
    """

    tool_name: str
    session_ids: tuple[str, ...]
    occurrence_count: int
    avg_grader_score: float | None


# LLM-style extractor signature: given a list of detected patterns +
# the journal entries that backed each, return draft ProposedSkills.
DraftExtractor = Callable[
    [list[_Pattern], list[JournalEntry]],
    "list[ProposedSkill] | Awaitable[list[ProposedSkill]]",
]


def noop_extractor(
    _patterns: list[_Pattern], _entries: list[JournalEntry],
) -> list[ProposedSkill]:
    """Default extractor — no LLM, no proposals. Bench-friendly default."""
    return []


@dataclass
class SkillProposer:
    """Walks Journal history → finds tool-use patterns → drafts
    skill candidates via a pluggable LLM extractor.

    Stateless over the journal directory. Construct fresh per run
    (e.g. once per dream cycle); discoveries are deterministic for a
    given on-disk journal snapshot.

    Parameters
    ----------
    reader : JournalReader
        Where to read past journal entries from.
    extractor_callable : DraftExtractor, default noop
        The actual LLM call (or any pattern→skill function). Async or
        sync supported.
    history_window : int, default 50
        Maximum entries to consider per ``propose()`` invocation.
    min_pattern_count : int, default 3
        A pattern must show up in this many distinct sessions to
        qualify for LLM drafting. Below 3 the signal is too noisy to
        be worth the LLM call.
    min_confidence : float, default 0.5
        Drafts below this confidence are dropped before returning.
    """

    reader: JournalReader
    extractor_callable: DraftExtractor = field(default=noop_extractor)
    history_window: int = 50
    min_pattern_count: int = 3
    min_confidence: float = 0.5

    # ── pattern discovery ────────────────────────────────────────────

    def detect_patterns(
        self, entries: list[JournalEntry],
    ) -> list[_Pattern]:
        """Count tool names across ``entries``; keep those occurring
        in ≥ ``min_pattern_count`` distinct sessions.

        Avg grader score is computed across all entries the pattern
        appeared in (None when no grader data — bench / unit-test
        sessions without graders running).
        """
        # Map tool_name → set of session_ids that used it.
        sessions_by_tool: dict[str, set[str]] = {}
        score_by_tool: dict[str, list[float]] = {}
        for e in entries:
            for tc in e.tool_calls:
                if not tc.name:
                    continue
                sessions_by_tool.setdefault(tc.name, set()).add(e.session_id)
                if e.grader_avg_score is not None:
                    score_by_tool.setdefault(tc.name, []).append(
                        e.grader_avg_score,
                    )

        out: list[_Pattern] = []
        for tool_name, sids in sessions_by_tool.items():
            if len(sids) < self.min_pattern_count:
                continue
            scores = score_by_tool.get(tool_name) or []
            avg = sum(scores) / len(scores) if scores else None
            out.append(_Pattern(
                tool_name=tool_name,
                session_ids=tuple(sorted(sids)),
                occurrence_count=len(sids),
                avg_grader_score=avg,
            ))

        # Newest-first by occurrence count is a reasonable default
        # priority — heaviest-used patterns first.
        out.sort(key=lambda p: p.occurrence_count, reverse=True)
        return out

    # ── public API ───────────────────────────────────────────────────

    async def propose(self) -> list[ProposedSkill]:
        """Run one detection→drafting pass over recent journal entries.

        Pure over the journal directory + extractor. Safe to call
        from a periodic task / dream cycle / CLI command.
        """
        entries = self.reader.recent(limit=self.history_window)
        if not entries:
            return []

        patterns = self.detect_patterns(entries)
        if not patterns:
            return []

        try:
            result = self.extractor_callable(patterns, entries)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 — extractor failures
            # must not crash the dream cycle; the next iteration
            # gets a fresh chance.
            _log.warning("proposer.extract_failed err=%s", exc)
            return []

        if not isinstance(result, list):
            _log.warning(
                "proposer.bad_extractor_return type=%s",
                type(result).__name__,
            )
            return []

        accepted: list[ProposedSkill] = []
        for proposed in result:
            if not isinstance(proposed, ProposedSkill):
                continue
            if proposed.confidence < self.min_confidence:
                continue
            accepted.append(proposed)
        return accepted
