"""StrategyDistiller — single-LLM-roundtrip ReasoningBank distillation.

Sprint 3 #6. Given a window of journal rows (mechanical session
metadata produced by :class:`JournalWriter`), prompt the LLM to emit
3-7 *distilled* strategies — each a "when X, then Y" pattern with the
session ids supporting it.

Honest-state guardrails (see ``docs/EVOLUTION_HONEST_STATE.md``):

* **Iron Rule #1 — Min 2 evidence.** The prompt explicitly tells the
  LLM to drop any pattern not supported by ≥ 2 distinct sessions.
  Defence in depth: we re-filter on the parse path so a hallucinating
  model can't sneak through a single-session strategy.

* **Iron Rule #2 — Confidence cap.** Whatever number the LLM emits
  (it loves to write 0.95) is passed through
  :func:`cap_confidence` so the value the bank stores is always
  bounded.

The distiller is **stateless** — it does not import a memory backend
or the bus. The caller (controller / scheduler — wired in a follow-up
PR) is responsible for handing the result to :class:`StrategyBank`.

Failure handling: a malformed LLM response (non-JSON, JSON missing
required fields, JSON not an array) returns ``[]`` after a warning
log. The distillation pass is best-effort — never raise into the
caller.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from xmclaw.core.journal.strategy_types import (
    MIN_EVIDENCE_COUNT,
    Strategy,
    cap_confidence,
    make_strategy_id,
)

_log = logging.getLogger(__name__)


_DISTILL_PROMPT_TEMPLATE = """\
You are distilling reusable strategies from a window of past sessions.
Each input row is one session's mechanical metadata (turn count, tool
calls, grader stats, anti-req violations).

Return JSON array of 3-7 strategies. Each entry:
  {{when_pattern, then_action, evidence_count, evidence_session_ids, confidence}}

Drop any pattern that >=2 sessions in this window do NOT both support.

Rules:
- when_pattern: short situation phrase (e.g. "user asks for refactor
  with no test")
- then_action: recommended next-turn action (e.g. "request a failing
  test before changing code")
- evidence_count: integer >= 2
- evidence_session_ids: array of session_id strings, length ==
  evidence_count, drawn from the input window
- confidence: float in [0.0, 1.0] reflecting how cleanly the pattern
  appeared across supporting sessions

Output JSON only — no prose, no markdown fences.

JOURNAL_WINDOW:
{journal_blob}
"""


def _build_prompt(journal_window: list[dict[str, Any]]) -> str:
    """Render the distillation prompt with the JSON-encoded window.

    We dump the full window as JSON rather than pretty-printing each
    row so the LLM sees a single structured blob. ``ensure_ascii=False``
    lets non-ASCII tool names / error strings round-trip without
    escape noise.
    """
    blob = json.dumps(journal_window, ensure_ascii=False, default=str)
    return _DISTILL_PROMPT_TEMPLATE.format(journal_blob=blob)


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    """Best-effort coerce LLM output into ``tuple[str, ...]``.

    Accepts list / tuple of any hashable; coerces each item via
    ``str()``. Returns empty tuple on non-iterable input so the
    upstream filter can drop the row.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _parse_one(item: Any) -> Strategy | None:
    """Parse a single LLM array entry into a Strategy.

    Returns ``None`` on any malformed shape or when evidence_count
    < :data:`MIN_EVIDENCE_COUNT`. Never raises; logs a debug line so
    aggregate parse failures show up under ``-v`` without polluting
    info logs.
    """
    if not isinstance(item, dict):
        _log.debug("strategy_distiller.parse: not a dict: %r", item)
        return None
    try:
        when = str(item["when_pattern"]).strip()
        then = str(item["then_action"]).strip()
        evidence_count = int(item["evidence_count"])
        evidence_session_ids = _coerce_str_tuple(
            item.get("evidence_session_ids", ())
        )
        confidence = float(item.get("confidence", 0.0))
    except (KeyError, TypeError, ValueError) as exc:
        _log.debug("strategy_distiller.parse: bad fields (%s): %r", exc, item)
        return None

    if not when or not then:
        return None
    # Iron Rule #1 defence: drop low-evidence rows even if the LLM
    # claimed otherwise. We use the larger of the declared count and
    # the actual session-id list length, so an inflated count alone
    # cannot smuggle a strategy through.
    actual_count = max(evidence_count, len(evidence_session_ids))
    if actual_count < MIN_EVIDENCE_COUNT:
        return None
    if len(evidence_session_ids) < MIN_EVIDENCE_COUNT:
        return None

    return Strategy(
        id=make_strategy_id(when, then),
        when_pattern=when,
        then_action=then,
        evidence_count=actual_count,
        evidence_session_ids=evidence_session_ids,
        # Iron Rule #2 defence: cap regardless of LLM output.
        confidence=cap_confidence(confidence),
        distilled_at=time.time(),
        last_retrieved_at=None,
    )


def _parse_response(raw: str) -> list[Strategy]:
    """Convert the raw LLM text into a list of Strategy.

    Tolerant: strips Markdown code fences if the model wrapped JSON
    in them, and accepts a top-level dict with a ``"strategies"`` key
    in addition to a bare array.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip optional language tag + closing fence.
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
        text = text.strip()

    try:
        decoded: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("strategy_distiller.parse_failed: %s", exc)
        return []

    if isinstance(decoded, dict):
        decoded = decoded.get("strategies", [])
    if not isinstance(decoded, list):
        _log.warning(
            "strategy_distiller.parse_failed: top-level not list: %r",
            type(decoded).__name__,
        )
        return []

    parsed: list[Strategy] = []
    for item in decoded:
        s = _parse_one(item)
        if s is not None:
            parsed.append(s)
    return parsed


class StrategyDistiller:
    """Distill journal windows into strategies via one LLM call.

    Parameters
    ----------
    llm:
        Anything with an ``async def complete(prompt: str, *,
        session_id: str) -> str`` method, where ``complete`` returns
        the raw model output. The exact LLM provider abstraction lives
        in ``xmclaw/providers/llm/``; we accept ``Any`` here to keep
        ``core/`` free of provider imports (Iron Rule from
        ``xmclaw/core/AGENTS.md`` §2).
    max_strategies:
        Cap on the number of strategies kept after parsing. Even when
        the LLM emits more, we trim to this. Default 7 matches the
        prompt's "3-7" upper bound.
    """

    def __init__(
        self, llm: Any, max_strategies: int = 7,
        evolution_tier: str = "unknown",
    ) -> None:
        self._llm = llm
        self._max = int(max_strategies)
        # Sprint 3 Iron Rule #3: ``evolution_tier`` is the result of
        # classifying the LLM's model id (``classify_model_tier`` in
        # xmclaw/providers/llm/_provider_profiles.py). When "weak",
        # distill_from_journal returns [] immediately without burning
        # an LLM call — Live-SWE-agent issue #7 + community reports
        # show weak models produce statistically-common phrases, not
        # useful patterns. Caller (factory) does the classification;
        # core/ stays free of provider imports.
        self._tier = (
            evolution_tier
            if evolution_tier in {"strong", "medium", "weak", "unknown"}
            else "unknown"
        )

    async def distill_from_journal(
        self,
        journal_window: list[dict[str, Any]],
        session_id: str = "_distill",
    ) -> list[Strategy]:
        """Run one LLM round-trip and return parsed strategies.

        ``journal_window`` is the input the controller hands us — a
        list of mechanical journal rows (already serialisable dicts).
        Empty input short-circuits to ``[]`` without an LLM call so
        the daemon's startup pass on a fresh install stays free.

        Iron Rule #3: weak-tier models return ``[]`` immediately —
        their strategy output is noise, not pattern. ``"unknown"``
        + ``"medium"`` + ``"strong"`` all run the LLM call.
        """
        if not journal_window:
            return []
        if self._tier == "weak":
            _log.info(
                "strategy_distiller.skipped_weak_tier session=%s "
                "journal_size=%d", session_id, len(journal_window),
            )
            return []

        prompt = _build_prompt(journal_window)
        # 2026-05-18: same shape as the planner / reasoning /
        # reflective_mutator fixes — production LLMProvider.complete
        # takes ``messages: list[Message]``, NOT a raw str + arbitrary
        # kwargs. The legacy call signature here would have produced
        # an AttributeError ('str' has no .role) on the first turn
        # any user with evolution.reasoning_bank.enabled tried this
        # path, which was then eaten by the broad except below.
        try:
            # Import Message from core.ir, not providers.llm.base —
            # core/ must not reach back into providers/ per the
            # import-direction rule (check_import_direction.py).
            from xmclaw.core.ir import Message
            messages = [Message(role="user", content=prompt)]
            resp = await self._llm.complete(messages)
        except TypeError:
            # Test mocks that still accept the legacy str + session_id
            # shape — fall through.
            try:
                resp = await self._llm.complete(
                    prompt, session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("strategy_distiller.llm_failed: %s", exc)
                return []
        except Exception as exc:  # noqa: BLE001 — best-effort surface
            _log.warning("strategy_distiller.llm_failed: %s", exc)
            return []

        # LLMResponse → .content; legacy mock → already a str.
        if isinstance(resp, str):
            raw = resp
        else:
            content_attr = getattr(resp, "content", None)
            raw = content_attr if isinstance(content_attr, str) else str(resp)

        strategies = _parse_response(raw)
        if len(strategies) > self._max:
            strategies = strategies[: self._max]
        return strategies


__all__ = ["StrategyDistiller"]
