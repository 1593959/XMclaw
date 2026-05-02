r"""LLM-backed extractor factories. Epic #24 Phase 3.5.

Phase 2 / 3 land the *harness* with no-op default extractors so the
plumbing is exercised even without an LLM configured. Phase 3.5
plugs real LLM calls in: ``build_skill_extractor`` for SkillProposer
(detects repeating tool patterns → drafts ProposedSkill), and
``build_profile_extractor`` for ProfileExtractor (reads recent
turns → drafts ProfileDelta).

Design constraints
------------------

* **Single LLM provider input** — both factories take an
  ``LLMProvider`` instance and call ``complete`` (non-streaming —
  short structured outputs don't benefit from streaming).
* **Strict JSON contract** — prompts ask for a JSON array only.
  Parser is tolerant: tries the raw output first, then strips
  markdown fences (``\`\`\`json … \`\`\``), then bails to ``[]``.
* **No silent corruption** — if the LLM returns malformed JSON or
  the wrong shape we log a warning and return ``[]``. The harness
  layer's confidence floor + min_pattern_count gates already
  protect against accidental low-quality drafts surviving.
* **Cap input size** — patterns / messages are truncated to a
  budget before going into the prompt so a degenerate input doesn't
  blow up the LLM call. Phase 3.5 picks conservative caps; tune on
  observed traffic.
* **Daemon-only**: this module imports `LLMProvider` from
  ``xmclaw/providers/llm/`` (DAG-respecting daemon→providers),
  which is exactly why it sits in ``daemon/`` and not ``core/``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from xmclaw.core.evolution import ProposedSkill
from xmclaw.core.evolution.proposer import _Pattern
from xmclaw.core.journal import JournalEntry
from xmclaw.core.profile import ProfileDelta
from xmclaw.providers.llm.base import LLMProvider, Message

_log = logging.getLogger(__name__)


# Generic JSON-array extraction tolerant of common LLM quirks.
_FENCE_RE = re.compile(
    r"```(?:json)?\s*([\s\S]+?)\s*```",
    re.IGNORECASE,
)


def _parse_json_array(text: str) -> list[Any]:
    """Best-effort JSON array parser.

    Tries ``json.loads`` on raw → on first fenced block → on slice
    from first ``[`` to last ``]``. Returns ``[]`` on failure.
    """
    text = (text or "").strip()
    if not text:
        return []

    # 1. Raw text.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2. First fenced block.
    m = _FENCE_RE.search(text)
    if m is not None:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Slice from first '[' to last ']'.
    lo = text.find("[")
    hi = text.rfind("]")
    if 0 <= lo < hi:
        try:
            parsed = json.loads(text[lo : hi + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    _log.warning("llm_extractor.parse_failed text=%s...", text[:120])
    return []


# ── SkillProposer extractor ─────────────────────────────────────────


_SKILL_SYSTEM_PROMPT = (
    "You are XMclaw's skill-pattern analyst. Given recent tool-use "
    "patterns and journal entries, draft any reusable SKILL.md "
    "candidates the agent should consider. ANSWER FORMAT: a JSON "
    "array. NO PROSE, NO MARKDOWN FENCES. Each element MUST have "
    "the exact shape:\n"
    "{\n"
    '  "skill_id": "auto-<verb>-<noun>[-<more>]",\n'
    '  "title": "short human title",\n'
    '  "description": "one-line description",\n'
    '  "body": "step-by-step procedure body",\n'
    '  "triggers": ["keyword1", "keyword2"],\n'
    '  "confidence": 0.85,\n'
    '  "evidence": ["session_id1", "session_id2"],\n'
    '  "source_pattern": "tool X used in 4 sessions"\n'
    "}\n"
    "B-169 skill_id naming rules (HARD):\n"
    "  - Starts with literal ``auto-`` so users can tell evolution-"
    "produced skills apart from curated/skills.sh ones.\n"
    "  - Kebab-case after the prefix: lowercase a-z, digits 0-9, "
    "hyphens only. NO dots, NO underscores, NO uppercase, NO spaces.\n"
    "  - At least TWO segments after ``auto-`` (verb + noun). "
    "``auto-bash`` is rejected; ``auto-bash-review`` is accepted.\n"
    "  - 12-60 chars total. Prefer verb-noun structure naming WHAT "
    "the skill DOES, not which tool it uses.\n"
    "  - Good: ``auto-summarise-failures``, ``auto-clean-pyc-files``, "
    "``auto-extract-flask-routes``, ``auto-write-pytest-fixtures``.\n"
    "  - Bad (rejected): ``auto.bash_review`` (dots/underscores), "
    "``BashReview`` (case + missing prefix), ``auto-bash`` (single "
    "segment), ``auto-do-it`` (vague), ``skill_42`` (no prefix).\n"
    "B-184 anti-redundancy (HARD — joint audit pain):\n"
    "  - DO NOT propose a skill that is a thin wrapper around a "
    "single built-in primitive tool. The agent already has these as "
    "first-class tools and they're better than any wrapper:\n"
    "    bash, list_dir, file_read, file_write, file_delete, "
    "glob_files, grep_files, apply_patch, sqlite_query, "
    "web_fetch, web_search, todo_write, todo_read.\n"
    "  - REJECT: ``auto-explore-file-system`` (= list_dir+bash), "
    "``auto-search-code-files`` (= grep_files+file_read), "
    "``auto-inspect-sqlite-db`` (= sqlite_query alone), "
    "``auto-run-shell-commands`` (= bash alone), "
    "``auto-read-matching-files`` (= glob_files+file_read).\n"
    "  - PROPOSE only when there's a real procedural sequence with "
    "domain decisions (when to do X vs Y), or when the workflow "
    "involves THIRD-party / non-primitive tools (skill_*, "
    "web_search+grader+remember chains, etc.). If the entire "
    "skill body would just be 'call <primitive_tool> with these "
    "args' — the agent's existing tool description already covers "
    "that, your skill adds noise.\n"
    "Confidence ∈ [0, 1]. Evidence MUST list at least one source "
    "session_id. If the patterns don't justify any new skill, return "
    "[]."
)


# B-169: enforce auto-kebab-case skill_id so evolution-produced skills
# read consistently with the skills.sh ecosystem (git-commit, find-skills,
# skill-creator). The dotted namespace lives in built-in Python skills
# (demo.read_and_summarize) and is a Python module convention; LLM-drafted
# Markdown skills should follow the dominant kebab look instead.
_AUTO_SKILL_ID_RE = re.compile(
    r"^auto-[a-z0-9]+(?:-[a-z0-9]+){1,5}$",
)
_SKILL_ID_MIN = 12
_SKILL_ID_MAX = 60


def _normalize_skill_id(raw: Any) -> str | None:
    """Coerce an LLM's ``skill_id`` to the ``auto-kebab-case`` convention.

    Strategy:
      1. Lowercase + strip.
      2. Strip common scheme prefixes the LLM might emit
         (``skill_``, ``skill-``, ``skill.``, ``auto_``, ``auto.``).
      3. Replace dots / underscores / whitespace with hyphens.
      4. Drop everything outside ``[a-z0-9-]``.
      5. Collapse repeated hyphens; trim leading/trailing.
      6. Re-prepend ``auto-``.
      7. Validate against :data:`_AUTO_SKILL_ID_RE` + length bounds.

    Returns ``None`` when the input can't be coerced to a valid id —
    the caller drops that proposal rather than registering a phantom.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    # Common LLM slips — strip schemey prefixes before normalising.
    for prefix in ("skill_", "skill-", "skill.", "auto_", "auto.", "auto-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = re.sub(r"[\s._]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        return None
    candidate = f"auto-{s}"
    if not (_SKILL_ID_MIN <= len(candidate) <= _SKILL_ID_MAX):
        return None
    if not _AUTO_SKILL_ID_RE.match(candidate):
        return None
    return candidate


def _format_patterns_for_prompt(
    patterns: list[_Pattern], entries: list[JournalEntry], *,
    max_patterns: int = 8, max_entries: int = 12,
) -> str:
    parts = ["## Patterns observed\n"]
    for p in patterns[:max_patterns]:
        avg = (
            f"avg_grader={p.avg_grader_score:.2f}"
            if p.avg_grader_score is not None else "no_grader_data"
        )
        parts.append(
            f"- tool `{p.tool_name}` used in {p.occurrence_count} sessions "
            f"({avg}); session_ids: {list(p.session_ids[:6])}"
        )

    parts.append("\n## Recent journal sample\n")
    for e in entries[:max_entries]:
        tools = ", ".join(t.name for t in e.tool_calls[:6])
        parts.append(
            f"- session={e.session_id} turns={e.turn_count} "
            f"tools=[{tools}] grader_avg="
            f"{e.grader_avg_score if e.grader_avg_score is not None else 'na'}"
        )
    return "\n".join(parts)


def _coerce_proposed_skill(raw: Any) -> ProposedSkill | None:
    """Convert one LLM JSON object into ProposedSkill or None.

    B-169: ``skill_id`` runs through :func:`_normalize_skill_id` before
    accepting the proposal. Slips like ``auto.bash_review`` get
    rewritten to ``auto-bash-review``; un-fixable ids (single segment,
    no alphanumerics, way too long, missing `auto-` after stripping)
    drop the proposal so we don't pollute the registry namespace.
    """
    if not isinstance(raw, dict):
        return None
    try:
        evidence = raw.get("evidence") or []
        if not isinstance(evidence, list) or not evidence:
            return None
        triggers = raw.get("triggers") or []
        if not isinstance(triggers, list):
            triggers = []
        sid = _normalize_skill_id(raw.get("skill_id"))
        if sid is None:
            _log.info(
                "llm_extractor.skill_id_rejected raw=%s — drops the "
                "proposal; LLM will retry on next dream tick.",
                str(raw.get("skill_id"))[:60],
            )
            return None
        return ProposedSkill(
            skill_id=sid,
            title=str(raw.get("title") or sid),
            description=str(raw.get("description") or ""),
            body=str(raw.get("body") or ""),
            triggers=tuple(str(t) for t in triggers),
            confidence=float(raw.get("confidence", 0.0)),
            evidence=tuple(str(e) for e in evidence),
            source_pattern=str(raw.get("source_pattern") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("llm_extractor.skill_coerce_failed err=%s raw=%s",
                     exc, str(raw)[:120])
        return None


def build_skill_extractor(
    llm: LLMProvider,
) -> Callable[
    [list[_Pattern], list[JournalEntry]],
    Awaitable[list[ProposedSkill]],
]:
    """Return an async extractor that calls ``llm`` to draft skill candidates.

    The returned callable matches ``SkillProposer.extractor_callable``'s
    signature. SkillProposer's ``min_confidence`` filter still runs
    on top of whatever confidence the LLM emits — we don't trust the
    model's self-rating to be calibrated.
    """

    async def extract(
        patterns: list[_Pattern], entries: list[JournalEntry],
    ) -> list[ProposedSkill]:
        if not patterns:
            return []
        user_prompt = _format_patterns_for_prompt(patterns, entries)
        try:
            t0 = time.perf_counter()
            resp = await llm.complete([
                Message(role="system", content=_SKILL_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ])
            elapsed = (time.perf_counter() - t0) * 1000.0
            _log.info(
                "llm_extractor.skill_call elapsed_ms=%.0f patterns=%d",
                elapsed, len(patterns),
            )
        except Exception as exc:  # noqa: BLE001 — LLM failures must
            # not crash the dream cycle; SkillDreamCycle catches its
            # own crashes too but defending the boundary here keeps
            # the extractor's contract clean.
            _log.warning("llm_extractor.skill_call_failed err=%s", exc)
            return []

        items = _parse_json_array(resp.content or "")
        out: list[ProposedSkill] = []
        for raw in items:
            p = _coerce_proposed_skill(raw)
            if p is not None:
                out.append(p)
        return out

    return extract


# ── ProfileExtractor extractor ──────────────────────────────────────


_PROFILE_SYSTEM_PROMPT = (
    "You are XMclaw's user-profile analyst. Given a recent transcript "
    "(user / assistant turns), surface any DURABLE preferences, "
    "constraints, communication styles, or habits that the agent "
    "should remember. ANSWER FORMAT: a JSON array. NO PROSE, NO "
    "MARKDOWN FENCES. Each element MUST be:\n"
    "{\n"
    '  "kind": "preference" | "constraint" | "style" | "habit",\n'
    '  "text": "one-line natural-language statement",\n'
    '  "confidence": 0.85\n'
    "}\n"
    "Only emit deltas that are likely to apply across future sessions. "
    "Single-task observations don't qualify. If nothing durable came "
    "up, return []."
)


def _format_messages_for_profile_prompt(
    messages: list[dict[str, Any]], max_turns: int = 12,
) -> str:
    pairs = messages[-(max_turns * 2):]
    parts = []
    for m in pairs:
        role = m.get("role", "?")
        content = (m.get("content") or "")[:600]
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _coerce_profile_delta(
    raw: Any, *, source_session_id: str, source_event_id: str, ts: float,
) -> ProfileDelta | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    kind = raw.get("kind")
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(kind, str) or not kind.strip():
        kind = "preference"
    try:
        return ProfileDelta(
            kind=str(kind),
            text=str(text),
            confidence=float(raw.get("confidence", 0.0)),
            source_session_id=source_session_id,
            source_event_id=source_event_id,
            ts=ts,
        )
    except (TypeError, ValueError) as exc:
        _log.warning("llm_extractor.profile_coerce_failed err=%s", exc)
        return None


def build_profile_extractor(
    llm: LLMProvider,
) -> Callable[
    [list[dict[str, Any]], dict[str, Any]],
    Awaitable[list[ProfileDelta]],
]:
    """Return an async extractor that calls ``llm`` to surface ProfileDeltas.

    The extractor matches ``ProfileExtractor.extractor_callable`` shape.
    The harness's ``min_confidence`` floor (default 0.5) drops
    low-confidence drafts before they hit USER.md.
    """

    async def extract(
        messages: list[dict[str, Any]], meta: dict[str, Any],
    ) -> list[ProfileDelta]:
        if not messages:
            return []
        user_prompt = _format_messages_for_profile_prompt(messages)
        try:
            t0 = time.perf_counter()
            resp = await llm.complete([
                Message(role="system", content=_PROFILE_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ])
            elapsed = (time.perf_counter() - t0) * 1000.0
            _log.info(
                "llm_extractor.profile_call elapsed_ms=%.0f messages=%d",
                elapsed, len(messages),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("llm_extractor.profile_call_failed err=%s", exc)
            return []

        items = _parse_json_array(resp.content or "")
        sid = str(meta.get("session_id") or "")
        eid = str(meta.get("last_user_event_id") or "")
        ts = time.time()
        out: list[ProfileDelta] = []
        for raw in items:
            d = _coerce_profile_delta(
                raw, source_session_id=sid, source_event_id=eid, ts=ts,
            )
            if d is not None:
                out.append(d)
        return out

    return extract
