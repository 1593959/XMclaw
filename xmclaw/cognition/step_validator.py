"""StepValidator — per-step "did this advance the goal" check.

Background
==========

Kimi K2.6 agent mode internally validates each tool result against
the active goal: if the result clearly doesn't make progress, it
re-plans rather than chaining another wrong step. That judgement
is trained into K2.6's weights.

This module externalises it: after every successful tool invocation
the AgentLoop can ask a small LLM "did THIS make progress toward
THIS goal?" and surface the verdict to the inner-monologue stream.
The model still decides what to do — we don't auto-replan — but
having an explicit advancement signal in context helps weak models
notice stalls earlier.

Cost model
==========

One extra LLM call per *successful* tool invocation. For 8-step
plans with 1-2 tools per step, that's 8-16 extra round-trips at
small-model latency (~300-800ms each). Optional, gated by
``enabled`` flag — default OFF until user opts in for high-stakes
runs.

Output
======

Validation is non-blocking. The verdict is published to the event
bus as ``INNER_MONOLOGUE`` payloads so it shows up in the UI's
think pane next to the tool output. Strict JSON shape so the UI
can render structured chips.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


_VALIDATOR_PROMPT = """\
You are a goal-progress auditor. The agent just completed a tool
call. Decide if the result MEASURABLY advances the goal.

GOAL:
{goal}

PLAN (if any):
{plan}

TOOL CALL JUST FINISHED:
  tool: {tool_name}
  args: {tool_args}
  result_excerpt: {tool_result}

Output strict JSON:

  {{"verdict": "advance" | "neutral" | "regress",
    "confidence": 0.0-1.0,
    "reason": "short single sentence"}}

GUIDANCE:
* "advance" = the result clearly moves the agent toward the goal
  (found needed info, made successful edit, executed planned step).
* "neutral" = the result is correct but the goal isn't materially
  closer (read a file that turned out unrelated; ran a command
  whose output the agent now needs to re-interpret).
* "regress" = the result actively contradicts the goal or wastes
  the next step (broke something, fetched the wrong thing,
  introduced a new error).

Rules:
* Strict JSON, no prose, no markdown fences.
* "confidence" must be a number in [0,1] — be honest, 0.5 if unsure.
* If you cannot tell from the snippet, say "neutral" with low
  confidence; do NOT guess "regress".
"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StepVerdict:
    """Verdict on a single tool-step."""

    verdict: str  # "advance" | "neutral" | "regress" | "skipped"
    confidence: float
    reason: str
    tool_name: str
    elapsed_ms: float


class StepValidator:
    """Run a one-shot validation LLM call after a successful tool.

    Constructor params:

    * ``llm`` — small/fast LLM. Same shape AgentLoop._llm uses.
    * ``timeout_s`` — wall-clock cap on the validator call. Default 4s.
    * ``max_result_chars`` — how much of the tool result to surface in
      the prompt. Default 800 chars.
    * ``enabled`` — kill switch. Default False (opt-in feature).
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        timeout_s: float = 4.0,
        max_result_chars: int = 800,
        enabled: bool = False,
    ) -> None:
        self._llm = llm
        self._timeout_s = max(1.0, float(timeout_s))
        self._max_result_chars = max(100, int(max_result_chars))
        self._enabled = bool(enabled)
        # Running counts surfaced via stats() for observability.
        self._advance = 0
        self._neutral = 0
        self._regress = 0
        self._failed = 0

    def set_llm(self, llm: Any) -> None:
        """Late-binding for factory-built validators."""
        self._llm = llm

    def set_enabled(self, enabled: bool) -> None:
        """Toggle at runtime — config reloads can flip this on/off."""
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled and self._llm is not None

    def stats(self) -> dict[str, int]:
        return {
            "advance": self._advance,
            "neutral": self._neutral,
            "regress": self._regress,
            "failed": self._failed,
        }

    async def validate(
        self,
        *,
        goal: str,
        plan_steps: list[str] | None,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        tool_result: str,
    ) -> StepVerdict | None:
        """Return a verdict, or ``None`` if validation is disabled / fails.

        Validator failures NEVER raise — the agent loop's hop should
        continue regardless of validator outcome. ``None`` means
        "validator silent on this step", which downstream renders as
        no verdict chip.
        """
        if not self.enabled:
            return None

        import time
        t0 = time.perf_counter()
        try:
            verdict = await asyncio.wait_for(
                self._do_validate(
                    goal=goal,
                    plan_steps=plan_steps,
                    tool_name=tool_name,
                    tool_args=tool_args or {},
                    tool_result=tool_result,
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.info(
                "step_validator.timeout tool=%s timeout=%.1fs",
                tool_name, self._timeout_s,
            )
            self._failed += 1
            return None
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "step_validator.failed tool=%s err=%s",
                tool_name, exc,
            )
            self._failed += 1
            return None

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if verdict is None:
            self._failed += 1
            return None

        v_str = str(verdict.get("verdict", "")).lower()
        if v_str not in ("advance", "neutral", "regress"):
            self._failed += 1
            return None

        try:
            conf = float(verdict.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        reason = str(verdict.get("reason", "")).strip()[:300]

        if v_str == "advance":
            self._advance += 1
        elif v_str == "neutral":
            self._neutral += 1
        else:
            self._regress += 1

        return StepVerdict(
            verdict=v_str,
            confidence=conf,
            reason=reason,
            tool_name=tool_name,
            elapsed_ms=elapsed_ms,
        )

    async def _do_validate(
        self,
        *,
        goal: str,
        plan_steps: list[str] | None,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: str,
    ) -> dict[str, Any] | None:
        from xmclaw.providers.llm.base import Message

        plan_block = "(no plan)"
        if plan_steps:
            plan_block = "\n".join(
                f"  {i + 1}. {s}" for i, s in enumerate(plan_steps[:10])
            )

        try:
            args_json = json.dumps(tool_args, ensure_ascii=False)[:400]
        except (TypeError, ValueError):
            args_json = str(tool_args)[:400]

        # Take head + tail of the tool result if it's long — middle is
        # usually less informative than the boundaries.
        result_excerpt = _excerpt(tool_result, self._max_result_chars)

        prompt = _VALIDATOR_PROMPT.format(
            goal=(goal or "(goal not provided)")[:500],
            plan=plan_block,
            tool_name=tool_name,
            tool_args=args_json,
            tool_result=result_excerpt,
        )
        resp = await self._llm.complete([Message(role="user", content=prompt)])
        raw = (getattr(resp, "content", "") or "").strip()
        return _parse_verdict_json(raw)


def _excerpt(text: str, max_chars: int) -> str:
    if not text:
        return "(empty result)"
    if len(text) <= max_chars:
        return text
    head = max_chars // 2 - 20
    tail = max_chars - head - 20
    return f"{text[:head]}\n...[truncated]...\n{text[-tail:]}"


def _parse_verdict_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    candidates = [raw]
    for m in _FENCE_RE.finditer(raw):
        candidates.append(m.group(1).strip())
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    return None


__all__ = ["StepValidator", "StepVerdict"]
