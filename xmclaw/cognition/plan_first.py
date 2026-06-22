"""PlanFirstGate — auto-decompose complex queries before hop_loop runs.

Background
==========

Kimi K2.6 agent mode internally plans before executing — "create a
market analysis" doesn't immediately call tools, it FIRST decides
what data is needed, in what order, then executes. That planning
step is trained into K2.6's weights via agentic RL.

This module externalises the same pattern: when a user query looks
"complex enough" (heuristic), we make ONE extra LLM call BEFORE the
hop_loop starts to produce a flat plan of 2–8 steps. The plan gets
stashed on the AgentLoop so GoalAnchor can re-inject it every N
hops + the planner-aware sanitiser can compute completion.

The result: weak models (Qwen 7B / Llama 8B) get the same
"think-before-act" discipline that's baked into Kimi K2.6's weights,
at the cost of one extra LLM call per complex turn.

Heuristic complexity classifier
===============================

We don't want to plan-prefix every "what time is it" — that'd waste
tokens + add latency. Cheap-to-evaluate heuristics:

  * length > ``min_chars`` (default 80) — short prompts rarely need
    planning
  * presence of multi-step markers (``first ... then``, ``并 ...
    然后``, numbered list ``1. ... 2. ...``, etc.)
  * presence of a "do these N things" connector (``and also``,
    ``另外``, ``再``)
  * mention of multiple distinct tool-ish verbs

Each match adds points; we plan when ``score >= threshold``.

This is intentionally conservative — the user can also force-plan
via ``ultrathink`` / explicit plan-mode UI toggle that already
exists (the agent_loop reads ``planMode`` from the chat state).

Output format
=============

We ask the LLM for strict JSON:

    [
      "Step 1: <verb> <object>",
      "Step 2: ...",
      ...
    ]

Tolerant parser accepts: raw JSON, fenced ```json blocks, or any
markdown list it falls back to. On total failure, returns an empty
list and the hop_loop just runs without a plan (graceful degrade —
no plan is always strictly worse than a bad plan).
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


_PLAN_PROMPT_TEMPLATE = """\
You are a task-decomposition assistant. The user's message below is a
complex multi-step request. Your ONLY job: produce a flat plan of 2 to
{max_steps} concrete steps, each describing ONE thing the agent should
do to make progress toward the goal.

Rules:
  * STRICT JSON array of strings. No prose, no markdown, no code fences.
  * Write every step in the SAME language as the user's message below.
    A Chinese request → Chinese steps; an English request → English
    steps. Never switch to English just because these instructions are
    in English. Match the user.
  * Each step ≤ 200 characters, imperative voice (e.g. "搜索 X" / "Search
    for X", "读取文件 Y" / "Read file Y", "汇总结果" / "Aggregate results").
  * Order matters — earlier steps unblock later ones.
  * If the request is actually simple (e.g. "what time is it"), return
    a 1-element list with the answer-shaped step, NOT a forced 2-step.
  * Do NOT include tool names or implementation details — those are
    chosen at execution time. Only describe the WORK to do.

User message:
\"\"\"
{user_message}
\"\"\"

Return JSON array now.
"""


# Multi-step markers — case-insensitive, mixed Chinese / English.
_MULTI_STEP_RES = [
    re.compile(r"\bfirst\b[\s\S]{1,200}?\bthen\b", re.IGNORECASE),
    re.compile(r"\bafter\b[\s\S]{1,200}?\bthen\b", re.IGNORECASE),
    re.compile(r"\bstep\s*1\b", re.IGNORECASE),
    # Numbered list across newlines (markdown style).
    re.compile(r"^\s*\d+[\.\)]\s.+\n\s*\d+[\.\)]\s", re.MULTILINE),
    # Inline numbered list "1. ... 2. ... 3. ..." (no newlines required).
    re.compile(r"(?:^|[\s\(\[])1[\.\)]\s.+?(?:^|[\s\(\[])2[\.\)]\s.+?(?:^|[\s\(\[])3[\.\)]"),
    re.compile(r"先[\s\S]{1,80}?[再然]后"),
    re.compile(r"首先[\s\S]{1,200}?(然后|再|接着|最后)"),
    re.compile(r"另外|此外"),
    re.compile(r"\band\s+also\b", re.IGNORECASE),
]

# English task verbs — \b works because they're Latin words.
_EN_VERBS_RE = re.compile(
    r"\b(search|find|read|write|analy[sz]e|aggregate|summari[sz]e|"
    r"compare|test|verify|review|generate|create|build|fetch|download|"
    r"upload|run|execute|install|deploy|fix|debug)\b",
    re.IGNORECASE,
)

# Chinese task verbs — \b doesn't work on CJK, so we match raw.
_CN_VERBS_RE = re.compile(
    r"(查找|搜索|读取|写入|生成|创建|分析|汇总|对比|测试|验证|"
    r"审查|修复|调试|安装|部署|下载|执行|运行|总结|检查|输出)",
)


class PlanFirstGate:
    """Heuristic-gated planner that runs ONCE before the hop loop.

    Constructor params:

    * ``llm`` — anything with ``async complete(messages) -> resp`` where
      ``resp.content`` is a string. Same shape AgentLoop._llm uses.
    * ``min_chars`` — messages shorter than this never trigger plan
      mode. Default 80 chars (about a tweet).
    * ``threshold`` — min heuristic score. Default 2 (one multi-step
      marker + one task-verb cluster, or two of either).
    * ``max_steps`` — cap planner output length. Default 8.
    * ``timeout_s`` — wall-clock cap on the planner call. Default 12s.
    """

    def __init__(
        self,
        *,
        llm: Any,
        min_chars: int = 80,
        threshold: int = 2,
        max_steps: int = 8,
        timeout_s: float = 25.0,
    ) -> None:
        self._llm = llm
        self._min_chars = max(20, int(min_chars))
        self._threshold = max(1, int(threshold))
        self._max_steps = max(2, int(max_steps))
        self._timeout_s = max(2.0, float(timeout_s))

    # ── Heuristic ─────────────────────────────────────────────────

    def is_complex(self, user_message: str) -> bool:
        """Score the message and return True iff it warrants planning.

        Costs nothing — pure regex. False-positives are OK (one extra
        LLM call); false-negatives are also OK (hop_loop runs without
        plan, which is the pre-Batch-B baseline).
        """
        if not isinstance(user_message, str):
            return False
        text = user_message.strip()
        if not text:
            return False

        # Short-circuit 1: very long messages auto-qualify regardless
        # of explicit markers. Users rarely write 400-char single-shot
        # questions.
        if len(text) >= 400:
            return True

        # Score-based path. We DON'T early-reject on min_chars before
        # checking multi-step markers — a strong "首先...然后" in a
        # 40-char Chinese message still warrants planning.
        score = 0
        for pat in _MULTI_STEP_RES:
            if pat.search(text):
                score += 1

        eng_hits = len(_EN_VERBS_RE.findall(text))
        cn_hits = len(_CN_VERBS_RE.findall(text))
        verb_hits = eng_hits + cn_hits

        if verb_hits >= 2:
            score += 1
        if verb_hits >= 4:
            score += 1

        # Length filter: if the message is too short AND has no
        # multi-step markers, suppress the verb-only signal — "find
        # and read foo.txt" is one operation, not a multi-step plan.
        if len(text) < self._min_chars and score < 2:
            return False

        return score >= self._threshold

    # ── Plan ──────────────────────────────────────────────────────

    async def plan(self, user_message: str) -> list[str]:
        """Decompose ``user_message`` into ≤ ``max_steps`` plan steps.

        Returns empty list on any failure (timeout, bad JSON, model
        error, no LLM wired). The hop_loop falls back to no-plan
        execution in that case — strictly no-worse than baseline.
        """
        if self._llm is None:
            return []

        from xmclaw.providers.llm.base import Message
        prompt = _PLAN_PROMPT_TEMPLATE.format(
            user_message=user_message[:2000],
            max_steps=self._max_steps,
        )
        try:
            resp = await asyncio.wait_for(
                self._llm.complete([Message(role="user", content=prompt)]),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "plan_first.timeout user_msg_len=%d", len(user_message),
            )
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_first.llm_failed err=%s", exc)
            return []

        raw = (getattr(resp, "content", "") or "").strip()
        steps = _parse_plan_steps(raw, max_steps=self._max_steps)
        if steps:
            logger.info(
                "plan_first.planned steps=%d msg_len=%d",
                len(steps), len(user_message),
            )
        return steps


_DEPS_PROMPT_TEMPLATE = """\
You are analyzing an ORDERED plan. For EACH step, list the indices of EARLIER
steps whose OUTPUT that step directly needs as input.

Rules:
  * STRICT JSON array of arrays — no prose, no markdown, no code fences.
  * Element i = list of 0-based indices (all < i) that step i depends on.
  * Only DIRECT data dependencies (step i consumes step j's result), NOT mere
    ordering or topic similarity. Steps that can run on their own → [].
  * Never reference an index >= the step's own index, and never itself.

Steps (index: text):
{numbered_steps}

Return the JSON array of arrays now (length must equal the number of steps).
"""


async def infer_plan_deps(
    llm: Any, steps: list[str], *, timeout_s: float = 12.0,
) -> list[list[int]]:
    """#2 DAG：给已按序拆出的 plan 步骤推断「每步依赖哪些前置步」。

    返回与 ``steps`` 等长的列表，``deps[i]`` 为 i 直接依赖的前置下标。
    任何失败（无 LLM / 超时 / 解析失败）→ 全空（= 全并行，安全退化，
    绝不比现状更差）。下游 executor 还会再做一次 d<i 规范化兜底。
    """
    if llm is None or not isinstance(steps, list) or len(steps) < 2:
        return [[] for _ in (steps or [])]
    from xmclaw.providers.llm.base import Message
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(steps))
    prompt = _DEPS_PROMPT_TEMPLATE.format(numbered_steps=numbered[:4000])
    try:
        resp = await asyncio.wait_for(
            llm.complete([Message(role="user", content=prompt)]),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_first.deps_infer_failed err=%s", exc)
        return [[] for _ in steps]
    raw = (getattr(resp, "content", "") or "").strip()
    return _parse_deps(raw, len(steps))


def _parse_deps(raw: str, n: int) -> list[list[int]]:
    """Tolerant parse of a JSON array-of-arrays. Fences stripped. On any
    failure returns all-empty. Only structural parsing here — the executor
    enforces the d<i invariant."""
    out: list[list[int]] = [[] for _ in range(n)]
    if not raw:
        return out
    candidates = [raw] + [m.group(1).strip() for m in _FENCE_RE.finditer(raw)]
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, list):
            for i in range(min(n, len(obj))):
                row = obj[i]
                if isinstance(row, list):
                    out[i] = [
                        d for d in row
                        if isinstance(d, int) and not isinstance(d, bool)
                    ]
            return out
    return out


# ── Tolerant parser ───────────────────────────────────────────────


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LIST_BULLET_RE = re.compile(
    r"^\s*(?:\d+[\.\)]\s*|[-*]\s+)(.+)$", re.MULTILINE,
)


def _parse_plan_steps(raw: str, *, max_steps: int) -> list[str]:
    """Tolerant 3-tier parser:

    1. Try raw JSON.
    2. Strip ```json fences then JSON.
    3. Fall back to markdown bullets / numbered list.

    Returns a clean list capped at ``max_steps``. Items > 240 chars
    get truncated. Empty list on total failure.
    """
    if not raw:
        return []

    candidates: list[str] = [raw]
    for m in _FENCE_RE.finditer(raw):
        candidates.append(m.group(1).strip())

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, list):
            steps = [
                _clean_step(s) for s in obj
                if isinstance(s, str) and s.strip()
            ]
            steps = [s for s in steps if s]
            if steps:
                return steps[:max_steps]
        # Object with a ``steps`` key — sometimes LLMs add envelope.
        if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
            steps = [
                _clean_step(s) for s in obj["steps"]
                if isinstance(s, str) and s.strip()
            ]
            steps = [s for s in steps if s]
            if steps:
                return steps[:max_steps]

    # Fallback — bullets / numbered list
    bullets = [
        _clean_step(m.group(1)) for m in _LIST_BULLET_RE.finditer(raw)
    ]
    bullets = [b for b in bullets if b]
    return bullets[:max_steps]


def _clean_step(s: str) -> str:
    s = s.replace("\r", "").strip()
    if not s:
        return ""
    if len(s) > 240:
        s = s[:239] + "…"
    return s


__all__ = ["PlanFirstGate"]
