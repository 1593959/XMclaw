"""MetaCognitionPass — periodic LLM scan over recent decision traces.

Looks at the last N traces; asks the LLM "what patterns do you see?".
The LLM returns structured ``Pattern`` envelopes that downstream
``Reformer`` can route into proposals (curriculum_edit / skill /
preference) — gated by the EvolutionController's grader contract so
hallucinated patterns don't self-mutate the agent.

Anti-overclaim safeguards:
  * Strict JSON schema with ``confidence_cap = 0.6`` — we never let
    the LLM claim more than "moderately confident" about a behavioural
    pattern. (Mirrors the Iron Rule #2 cap from ReasoningEngine.)
  * Minimum evidence requirement — pattern needs ≥ 3 supporting
    traces. Single-instance "patterns" are noise.
  * Outcome filter — patterns where ALL evidence has
    ``outcome == "ok"`` get auto-rejected. We're looking for
    failure modes, not "things that worked".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

CONFIDENCE_CAP = 0.6


PatternKind = Literal[
    "repeated_failure",      # tool / skill keeps failing same way
    "user_pushback_pattern", # user repeatedly disagrees with style
    "missed_opportunity",    # we keep NOT calling a relevant tool
    "decline_overuse",       # we decline too eagerly
    "answer_style_mismatch", # we keep going long when user wants short, etc.
]


@dataclass(frozen=True, slots=True)
class Pattern:
    """One behavioural pattern the LLM extracted from traces.

    Fields:
        kind           — categorical pattern type
        summary        — short human-readable description
        evidence       — list of trace ids supporting the pattern
        confidence     — clamped to [0, 0.6]
        suggestion     — what would help (free text; the Reformer
                          decides what concrete proposal to make)
        recurrence     — how many times the pattern instance shows
                          up in the trace window (informational)
    """
    kind: PatternKind
    summary: str
    evidence: tuple[str, ...]
    confidence: float
    suggestion: str
    recurrence: int


_SCAN_PROMPT = """\
你是一个观察 agent 行为的元认知分析器。

下面是 agent 最近 {n} 个决策的痕迹 (越前面越早)。每条包含:
- ts (时间)
- kind (决策种类: tool_choice / skill_choice / answer_style / ...)
- chosen (选择了什么)
- alternatives (放弃的选项)
- reason (理由)
- outcome (结果: ok / error / user_pushed_back / unknown)

任务: 找出**重复出现的失败模式或可改进点** (不是单次事件)。每个模式
必须有 **至少 3 条** 痕迹支持，并且**不能全部 outcome=ok** (我们关心
的是问题，不是已经在 work 的事)。

模式种类 (kind 必须是这五种之一):
- repeated_failure        某 tool/skill 反复用同样的方式失败
- user_pushback_pattern   用户反复对某种风格表示不满
- missed_opportunity      存在相关 tool 但 agent 没用
- decline_overuse         agent 过早 decline，本可以做的也不做
- answer_style_mismatch   回答长度/语气与用户期望持续不符

输出严格 JSON 列表 (没有就 []):

[
  {{
    "kind": "repeated_failure",
    "summary": "<一句话>",
    "evidence": ["<trace_id1>", "<trace_id2>", "<trace_id3>"],
    "confidence": <0.0-0.6 之间的浮点>,
    "suggestion": "<怎么改善>",
    "recurrence": <int>
  }},
  ...
]

判断准则:
- confidence 严格不超过 0.6 — 即使非常确定也要克制 (Iron Rule #2)。
- evidence 至少 3 条 trace_id; 否则不要纳入这个模式。
- 不输出 JSON 外字符。

痕迹:
{traces_block}
"""


class MetaCognitionPass:
    """Run a single metacognition pass.

    Args:
        llm: ``async complete(messages, tools=None) -> LLMResponse``.
        recorder: ``DecisionTraceRecorder`` (or duck-typed). Pass-only
            calls ``recorder.recent(limit=N)``.
        min_evidence: minimum trace_id count for a Pattern to ship.
            Default 3 — singletons are noise.
        timeout_s: hard wall-clock cap on the LLM call.
    """

    def __init__(
        self,
        *,
        llm: Any,
        recorder: Any,
        min_evidence: int = 3,
        timeout_s: float = 20.0,
    ) -> None:
        self._llm = llm
        self._recorder = recorder
        self._min_evidence = max(1, int(min_evidence))
        self._timeout_s = max(2.0, float(timeout_s))

    async def run(self, *, lookback: int = 100) -> list[Pattern]:
        """Pull the last ``lookback`` traces, ask the LLM for
        patterns, return the surviving ones (passing min_evidence,
        outcome filter, kind validation, confidence cap)."""
        traces = self._recorder.recent(limit=lookback)
        if len(traces) < self._min_evidence:
            return []

        # Don't waste LLM call when EVERY trace is "ok" — there's
        # no failure pattern to find by definition.
        non_ok = [t for t in traces if t.outcome != "ok"]
        if len(non_ok) < self._min_evidence:
            return []

        prompt = _SCAN_PROMPT.format(
            n=len(traces),
            traces_block=self._format_traces(traces),
        )
        envelope = await self._ask_llm(prompt)
        if envelope is None:
            return []

        patterns: list[Pattern] = []
        # Build a set of valid trace ids for evidence sanity check.
        valid_ids = {t.id for t in traces}
        # Outcome lookup for the all-ok rejection filter.
        outcome_by_id = {t.id: t.outcome for t in traces}

        for item in envelope:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()
            if kind not in (
                "repeated_failure", "user_pushback_pattern",
                "missed_opportunity", "decline_overuse",
                "answer_style_mismatch",
            ):
                continue
            evidence_raw = item.get("evidence", [])
            if not isinstance(evidence_raw, list):
                continue
            evidence = [
                str(e) for e in evidence_raw
                if isinstance(e, str) and e in valid_ids
            ]
            if len(evidence) < self._min_evidence:
                continue
            # All-ok evidence → reject (no real failure to learn from).
            if all(
                outcome_by_id.get(e, "unknown") == "ok"
                for e in evidence
            ):
                continue
            summary = str(item.get("summary", "")).strip()
            if not summary:
                continue
            try:
                conf = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = max(0.0, min(conf, CONFIDENCE_CAP))
            try:
                rec = int(item.get("recurrence", len(evidence)))
            except (TypeError, ValueError):
                rec = len(evidence)
            patterns.append(Pattern(
                kind=kind,  # type: ignore[arg-type]
                summary=summary[:500],
                evidence=tuple(evidence),
                confidence=conf,
                suggestion=str(item.get("suggestion", ""))[:500],
                recurrence=max(1, rec),
            ))

        return patterns

    # ── Internals ────────────────────────────────────────────────

    @staticmethod
    def _format_traces(traces: list[Any]) -> str:
        lines: list[str] = []
        for t in traces:
            try:
                alts = ", ".join((t.alternatives or [])[:3])
            except Exception:  # noqa: BLE001
                alts = ""
            line = (
                f"[{t.id[:8]}] ts={int(t.ts)} kind={t.kind} "
                f"chosen={t.chosen!r} "
                f"alts=[{alts}] "
                f"outcome={t.outcome} "
                f"reason={t.reason!r}"
            )
            if t.outcome_note:
                line += f" note={t.outcome_note!r}"
            if len(line) > 400:
                line = line[:397] + "..."
            lines.append(line)
        return "\n".join(lines)

    async def _ask_llm(self, prompt: str) -> Any | None:
        import asyncio
        try:
            # 2026-05-10 import-direction fix: previously imported
            # ``xmclaw.providers.llm.base.Message`` here, which violates
            # the ``core cannot import from providers`` rule
            # (scripts/check_import_direction.py). The LLM consumer is
            # duck-typed against ``role`` + ``content`` attributes, so
            # a tiny local dataclass works just as well and keeps
            # core/ free of provider deps.
            from dataclasses import dataclass

            @dataclass(frozen=True, slots=True)
            class _Msg:
                role: str
                content: str

            resp = await asyncio.wait_for(
                self._llm.complete([
                    _Msg(role="user", content=prompt),
                ]),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("metacognition.llm_timeout")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("metacognition.llm_failed err=%s", exc)
            return None

        content = (getattr(resp, "content", "") or "").strip()
        if content.startswith("```"):
            content = content.lstrip("`")
            if content.lower().startswith("json"):
                content = content[4:]
            content = content.strip("`").strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(
                "metacognition.bad_json preview=%r", content[:200],
            )
            return None
        if not isinstance(data, list):
            return None
        return data


__all__ = ["MetaCognitionPass", "Pattern", "PatternKind", "CONFIDENCE_CAP"]
