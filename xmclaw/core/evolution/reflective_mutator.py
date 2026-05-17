"""GEPA-style reflective mutator — single-round-trip self-critique mutation.

Background
----------
The DSPy-backed :class:`SkillMutator` (see :mod:`mutator`) wraps
``dspy.GEPA().compile()`` over a held-out dataset. That works when we
have a labelled corpus, but real evolution traffic is grader verdicts
on *recent failures*, not pre-labelled examples — and DSPy is an
optional third-party dependency.

This module implements the GEPA paper's reflective mutation step
without DSPy: one LLM round-trip that consumes ``(head_source,
recent_failures)`` and proposes 1-3 alternative source candidates with
natural-language critiques. The output drops malformed JSON entries
silently and caps every confidence at 0.6 (we have no ground truth at
proposal time — the live grader is the authority).

Public API
----------
* :class:`MutationCandidate` — frozen dataclass; one proposal.
* :class:`ReflectiveMutator.propose_mutations` — async; returns ≤
  ``max_per_skill`` candidates. Never raises.

Design notes
------------
* **Single round-trip.** Multi-step reflection chains are out of scope
  here — we want one prompt, one parse. Cost predictability matters
  more than marginal quality at this layer.
* **Confidence cap = 0.6.** Aligned with the Honest Grader's principle
  that without runtime evidence a candidate cannot self-claim a high
  score (see ``docs/EVOLUTION_HONEST_STATE.md`` Iron Rules — agent
  self-assessment is bounded above by external verification).
* **Best-effort.** Any LLM error, JSON parse failure, or schema
  mismatch yields an empty list — never an exception. Upstream callers
  (the controller / orchestrator) treat empty as "no proposal this
  cycle" without crashing the loop.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

# Confidence ceiling — see module docstring rationale.
_CONFIDENCE_CAP: float = 0.6

# Hard upper bound on candidates we'll keep from a single LLM response,
# even when ``max_per_skill`` is set higher. The prompt asks for ≤3,
# so anything beyond that is the LLM hallucinating.
_HARD_RESPONSE_CAP: int = 3


@dataclass(frozen=True)
class MutationCandidate:
    """One reflective-mutation proposal.

    Attributes:
        skill_id: target skill identifier.
        parent_version: integer version this candidate forks from.
        proposed_source: the new SKILL body text. Trimmed; empty
            strings are dropped before this dataclass is constructed.
        reflection_summary: the LLM's natural-language critique of the
            parent — why the change is proposed.
        confidence: in ``[0.0, _CONFIDENCE_CAP]``. The cap is enforced
            by :meth:`ReflectiveMutator.propose_mutations`.
        created_at: ``time.time()`` at construction.
    """
    skill_id: str
    parent_version: int
    proposed_source: str
    reflection_summary: str
    confidence: float
    created_at: float


_PROMPT_TEMPLATE = """You are reviewing a skill prompt that has been failing on \
recent tasks. Reflect on what went wrong and propose 1-3 alternative \
versions of the skill source that might do better.

HEAD source:
---
{head_source}
---

Recent failure traces (JSON):
{failures_json}

Output ONLY a JSON array (no prose, no code fences). Each entry has:
  - "proposed_source": string, the new full skill body.
  - "reflection_summary": string, why you changed what you changed.
  - "confidence": float in [0, 1], your subjective belief this fix helps.

Example shape:
[{{"proposed_source": "...", "reflection_summary": "...", "confidence": 0.4}}]
"""


def _build_prompt(head_source: str, recent_failures: list[dict[str, Any]]) -> str:
    """Assemble the single-round-trip reflection prompt.

    Failures are JSON-serialised with ``default=str`` so non-trivial
    payloads (timestamps, enums) don't blow up the encoder. Truncated
    to keep the context window predictable.
    """
    try:
        failures_json = json.dumps(
            recent_failures[:20], default=str, ensure_ascii=False, indent=2
        )
    except (TypeError, ValueError):
        # Pathological payload — fall back to ``str`` of the list.
        failures_json = str(recent_failures[:20])
    return _PROMPT_TEMPLATE.format(
        head_source=head_source, failures_json=failures_json
    )


def _extract_json_array(raw: str) -> list[Any]:
    """Best-effort recovery of a JSON array from raw LLM output.

    Handles three common failure modes:

    1. Pure JSON — happy path.
    2. JSON wrapped in `````json ... ````` fences.
    3. Prose preamble before the first ``[``.

    Returns ``[]`` on any unrecoverable shape so callers don't have to
    branch on the parse failure.
    """
    if not raw or not raw.strip():
        return []
    txt = raw.strip()

    # Strip code fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", txt, re.DOTALL)
    if fence:
        txt = fence.group(1).strip()

    # Try direct parse first.
    try:
        parsed = json.loads(txt)
    except (TypeError, ValueError):
        parsed = None

    if parsed is None:
        # Find the first balanced JSON array inside the text.
        start = txt.find("[")
        if start < 0:
            return []
        depth = 0
        end = -1
        for i in range(start, len(txt)):
            ch = txt[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return []
        try:
            parsed = json.loads(txt[start : end + 1])
        except (TypeError, ValueError):
            return []

    if not isinstance(parsed, list):
        return []
    return parsed


async def _call_llm(llm: Any, prompt: str) -> str:
    """Adapt to whatever shape the injected ``llm`` exposes.

    We accept any of the following — covers our internal ``LLMProvider``
    ABC plus typical mocks used in tests:

    * ``LLMProvider.complete(messages: list[Message]) -> LLMResponse``
      (the production shape since Wave-27; ``.complete`` accepts
      ``list[Message]`` not ``str``)
    * ``async llm.acomplete(prompt: str) -> str``
    * ``async llm.complete(prompt: str) -> str`` (legacy test mocks)
    * ``llm.complete(prompt: str) -> str`` (sync)
    * ``async llm(prompt) -> str`` / ``llm(prompt) -> str``

    Whatever the call returns, we coerce to ``str``. If we got back an
    ``LLMResponse`` with a ``.content`` attribute, use that — otherwise
    ``str(result)``. Exceptions propagate; :meth:`propose_mutations`
    catches them and returns an empty candidate list.

    2026-05-18 fix: pre-fix this function passed ``prompt`` (a ``str``)
    directly into ``llm.complete``. Production providers
    (anthropic/openai adapters) iterate the ``messages`` argument
    looking for ``.role``, so a raw string blew up with
    ``'str' object has no attribute 'role'``. The signature change
    on LLMProvider.complete predated this module's adapter, and the
    crash was masked by ``propose_mutations``'s broad except → empty
    candidates. Same shape as the planner.py / reasoning.py fixes
    (d664e5c, 69dd843).
    """
    for attr in ("acomplete", "complete", "generate", "agenerate"):
        fn = getattr(llm, attr, None)
        if fn is None:
            continue
        # Try the modern list[Message] shape first; fall back to
        # legacy ``fn(prompt)`` on TypeError so test mocks that still
        # accept a raw str keep working. Import Message from core.ir
        # (not providers.llm.base) to stay on the right side of the
        # import-direction rule — see Wave-29 2026-05-18 commit.
        try:
            from xmclaw.core.ir import Message
            messages = [Message(role="user", content=prompt)]
            result = fn(messages)
        except TypeError:
            result = fn(prompt)
        # If the call returned an awaitable, await it.
        if hasattr(result, "__await__"):
            try:
                result = await result
            except TypeError:
                # Awaited form rejected list[Message]; legacy shape
                # may accept the raw prompt.
                result = fn(prompt)
                if hasattr(result, "__await__"):
                    result = await result
        if result is None:
            return ""
        # LLMResponse-shaped → unwrap .content (str). Anything else →
        # str(...) coercion as before.
        content_attr = getattr(result, "content", None)
        if isinstance(content_attr, str):
            return content_attr
        return str(result)
    # Fall back to calling ``llm`` itself.
    if callable(llm):
        result = llm(prompt)
        if hasattr(result, "__await__"):
            result = await result
        return "" if result is None else str(result)
    raise TypeError(
        f"unsupported llm shape: {type(llm).__name__} has no acomplete/"
        "complete/generate/__call__"
    )


class ReflectiveMutator:
    """GEPA-style reflective mutator. Construct once, reuse per evolution loop.

    Args:
        llm: any object exposing ``acomplete`` / ``complete`` / ``__call__``
            that consumes a prompt string and returns a string. Tests
            inject a mock; production wires the daemon's LLMProvider.
        max_per_skill: hard ceiling on candidates returned per call.
            Defaults to 5; the prompt itself asks for ≤3, so this is
            mainly a safety net against hallucinated overruns.
    """

    def __init__(
        self, llm: Any, max_per_skill: int = 5,
        evolution_tier: str = "unknown",
    ) -> None:
        self._llm = llm
        self._max_per_skill = max(1, int(max_per_skill))
        # Sprint 3 Iron Rule #3: weak-tier models produce noise on
        # mutation prompts (Live-SWE issue #7). When evolution_tier is
        # "weak", propose_mutations returns [] immediately. Caller
        # (factory) does the classification; core/ stays free of
        # provider imports.
        self._tier = (
            evolution_tier
            if evolution_tier in {"strong", "medium", "weak", "unknown"}
            else "unknown"
        )

    async def propose_mutations(
        self,
        skill_id: str,
        head_source: str,
        recent_failures: list[dict[str, Any]],
        context_signature: str = "default",  # noqa: ARG002 — reserved for future routing
        parent_version: int = 0,
    ) -> list[MutationCandidate]:
        """Single-round-trip reflective mutation.

        Returns a (possibly empty) list of candidates. Never raises.

        Empty failure list short-circuits to ``[]`` — there is nothing
        to reflect on, so we don't burn an LLM call.

        Iron Rule #3: weak-tier models return ``[]`` immediately —
        mutation prompts are noise on weak models.

        Malformed entries (missing ``proposed_source``, non-string
        types, empty source body, non-numeric confidence) are silently
        dropped. The remaining candidates have their ``confidence``
        clamped to ``[0.0, _CONFIDENCE_CAP]``.
        """
        if not recent_failures:
            return []
        if self._tier == "weak":
            _log.info(
                "reflective_mutator.skipped_weak_tier skill=%s "
                "failures=%d", skill_id, len(recent_failures),
            )
            return []

        prompt = _build_prompt(head_source, recent_failures)
        try:
            raw = await _call_llm(self._llm, prompt)
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning(
                "reflective_mutator.llm_failed skill=%s err=%s",
                skill_id,
                exc,
            )
            return []

        items = _extract_json_array(raw)
        candidates: list[MutationCandidate] = []
        now = time.time()
        cap = min(self._max_per_skill, _HARD_RESPONSE_CAP)
        for item in items:
            if len(candidates) >= cap:
                break
            cand = _build_candidate(item, skill_id, parent_version, now)
            if cand is not None:
                candidates.append(cand)
        return candidates


def _build_candidate(
    item: Any, skill_id: str, parent_version: int, now: float
) -> MutationCandidate | None:
    """Validate one parsed JSON entry and build a candidate, or ``None``.

    Strict but quiet — any shape mismatch returns ``None`` rather than
    raising, so the caller can drop the entry without flow-of-control
    gymnastics.
    """
    if not isinstance(item, dict):
        return None
    src = item.get("proposed_source")
    if not isinstance(src, str):
        return None
    src = src.strip()
    if not src:
        return None
    summary = item.get("reflection_summary", "")
    if not isinstance(summary, str):
        summary = str(summary)
    raw_conf = item.get("confidence", 0.0)
    try:
        conf = float(raw_conf)
    except (TypeError, ValueError):
        return None
    # Clamp into [0, cap]. NaN-safe via the explicit comparison.
    if conf != conf:  # NaN check
        return None
    conf = max(0.0, min(conf, _CONFIDENCE_CAP))
    return MutationCandidate(
        skill_id=skill_id,
        parent_version=parent_version,
        proposed_source=src,
        reflection_summary=summary,
        confidence=conf,
        created_at=now,
    )
