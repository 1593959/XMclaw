"""LLM Fact Extractor — Layer 2 of the v2 memory write pipeline.

KeyInfoExtractor (Layer 1, regex, ~20 patterns) catches structured
shapes: URLs / accounts / phones / dates / etc. But regex can't see:

  * Implicit identity ("我们做电商" → industry=ecommerce)
  * Paraphrased deadlines ("月底前 / 下个月初 / 30 天内")
  * Disambiguated numbers (5万 = 业务目标 vs 5万 = 客户人数)
  * Cross-sentence references ("他" → 上文提到的人)
  * Soft preferences ("不喜欢太啰嗦的回答" — no "我喜欢" keyword)
  * Domain knowledge ("电商" → 业务模型 + 用户画像)

LLMFactExtractor closes this gap. Per-message LLM call returns a
structured JSON array of facts. Runs ASYNC after the user turn
finishes so it doesn't add latency.

Architecture:
  user_message
    → Layer 1 sync regex (guarantees URL/account/numeric land NOW)
    → Layer 2 async LLM (semantic + paraphrase + implicit facts)
    → both write to MemoryService.remember (idempotent on text →
      same fact written twice just bumps evidence_count)

Confidence comes from the LLM's own estimate clamped to
[0.5, 0.95] — regex stays at the 0.78-0.95 high end since its
precision is higher.

The prompt enumerates 6 kinds (preference / decision / identity /
commitment / correction / project) because those are what users
typically *say* in chat. ``episode`` is auto-tagged elsewhere
(successful problem-solving events) and ``lesson`` lands via the
separate ExtractLessonsHook post-turn path, so this user-message
extractor doesn't suggest them.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# ── Prompt ───────────────────────────────────────────────────────


def _build_extract_prompt(user_message: str) -> str:
    """2026-05-28 memory v3 phase 1.4: bucket choices are now
    rendered from the central ``buckets.BUCKETS`` registry so the
    extractor prompt stays automatically in sync when buckets are
    added/removed/edited. Pre-v3, bucket assignment lived as
    hardcoded if/elif in the Python caller — which silently dropped
    any fact that didn't match the 3 cases (the "dark facts" bug).
    """
    from xmclaw.memory.v2.buckets import render_for_prompt
    return _EXTRACT_PROMPT_TEMPLATE.format(
        user_message=user_message,
        bucket_choices=render_for_prompt(),
    )


_EXTRACT_PROMPT_TEMPLATE = """\
你是一个事实抽取器。从下面这一条用户消息里抽取出**所有**值得长期\
记住的事实/偏好/决定/约束。

用户消息：
{user_message}

输出严格 JSON 数组，每条事实形如：

{{
  "text": "事实的陈述句 (一句话，越紧凑越好)",
  "kind": "preference | decision | identity | commitment | correction | project | lesson",
  "scope": "user | project | session",
  "bucket": "<见下方 bucket 选项，必填>",
  "confidence": 0.0-1.0
}}

kind 含义：
- preference: 用户偏好（"喜欢简短回复"、"用 PowerShell 不用 bash"）
- decision: 已做的决定（"决定用 LanceDB"）
- identity: 身份/身份相关事实（"用户做电商生意"、"Windows 11"、"用户 25 岁"）
- commitment: 待办/承诺/截止（"agent 下次写测试"、"月底前上线"）
- correction: 纠正信号（"不要再 X / 永远 Y / 必须 Z"）
- project: 项目参数 (网址/账号/数字目标/技术栈/团队信息)
- lesson: 经验教训 / 工作流洞察（"下次应该先备份再改"、"X 工具在 Y 场景下会失败"）

scope 含义：
- user: 长期跨项目的事实（个人偏好、身份、人际关系）
- project: 当前项目的事实（URL/账号/目标/截止）
- session: 仅本会话相关，不应长期保留

{bucket_choices}

判断准则：
- text 必须是**事实陈述句**，不是问题或闲聊
- 跨越多个事实的复杂消息应拆成多条
- 不要复述消息原文，要**归纳**成简洁陈述
- 拿不准时**宁可输出**（remember 是幂等的），不要漏
- **bucket 字段必填**，无法分类时填 "misc"（不要填空字符串）
- 闲聊 / 单次操作细节 / 临时上下文 → 不要输出
- 输出 JSON 数组之外**不要**任何字符（包括代码块标记）

如果没有任何值得长期保留的事实，输出空数组 []。
"""


# ── Extracted fact shape ─────────────────────────────────────────


_VALID_KINDS = {
    "preference", "decision", "identity",
    "commitment", "correction", "project", "lesson",
}
_VALID_SCOPES = {"user", "project", "session"}


# ── Typed candidate (Phase 7 shim #7) ───────────────────────────


@dataclass(slots=True, frozen=True)
class LLMCandidate:
    """One un-remembered candidate fact from ``extract_candidates``.

    Phase 7 V1→V2 shim. V1's ``MemoryExtractor.extract`` returned a
    single typed ``ExtractedFact`` (or None); callers (notably
    ``hop_loop._bg_extract_and_put``) then decided whether to call
    ``unified_memory.put``. V2's ``extract`` returned a list[dict] —
    same data, no types. This dataclass closes the type gap so the
    "extract → decide → remember" pattern works the same as V1.

    Fields mirror ``MemoryService.remember`` kwargs so callers can
    do ``await svc.remember(**asdict(candidate))`` (or pass through
    individually) without remapping.
    """

    text: str
    kind: str
    scope: str
    confidence: float
    bucket: str = ""


# ── Extractor ────────────────────────────────────────────────────


class LLMFactExtractor:
    """LLM-driven fact extractor running async after each turn.

    Args:
        llm: any async LLM with ``.complete(messages, tools=None) ->
            LLMResponse``. Reuses the main agent LLM by default but
            production should pass a cheap+fast model (haiku /
            kimi-flash).
        timeout_s: hard wall-clock cap. Default 30s — same as
            MemoryExtractor (Wave 26 fix-4 raised from 8s).
        max_concurrent: at-most-N extractions in flight at any time
            across the whole process. Smoke-test revealed that
            multiple concurrent extracts can saturate the LLM
            channel and starve the main turn's reply LLM call.
            Default 1: serialise extracts; the main turn always gets
            priority. Skipped extracts return [] without firing the
            LLM — idempotent regex layer already covers the high-
            precision patterns, and the next turn will catch any
            missed semantic facts on its own extract pass.
        log: logger (defaults to module logger).
    """

    # Class-level semaphore so the limit is GLOBAL across all
    # LLMFactExtractor instances (one daemon ⇒ one cap). The agent's
    # reply LLM call lives outside this semaphore — extracts never
    # block the user-visible turn.
    _global_sem: asyncio.Semaphore | None = None

    def __init__(
        self,
        llm: Any,
        *,
        timeout_s: float = 30.0,
        max_concurrent: int = 1,
        log: Any = None,
    ) -> None:
        self._llm = llm
        self._timeout_s = max(1.0, float(timeout_s))
        self._max_concurrent = max(1, int(max_concurrent))
        # Lazy semaphore init — must happen inside a running event
        # loop, not at module import time.
        if LLMFactExtractor._global_sem is None:
            LLMFactExtractor._global_sem = asyncio.Semaphore(
                self._max_concurrent,
            )
        self._log = log or _log

    async def extract(
        self,
        user_message: str,
        assistant_response: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a list of fact dicts. Empty list on any failure —
        never raises (background path can't kill the turn).

        ``assistant_response`` (Phase 7 shim #7): optional assistant
        turn text. When provided, the prompt includes it as context
        so the extractor can pick up facts visible only in the
        assistant's reply (e.g. agent confirms a number / decision
        the user implied). Mirrors V1 ``MemoryExtractor.extract``'s
        2-arg signature.
        """
        if not user_message or not user_message.strip():
            return []
        # Skip extremely short messages (likely "好的" / "ok" — no
        # facts worth LLM cost).
        if len(user_message.strip()) < 8:
            return []

        if assistant_response and assistant_response.strip():
            # Append the assistant turn as additional context after
            # the user message. The extractor is still primarily
            # about the user's message — assistant context is
            # supplementary (helps disambiguate references / picks
            # up agent-confirmed facts).
            combined = (
                f"{user_message[:2000]}\n\n"
                f"[助手回复，仅作上下文参考]：\n"
                f"{assistant_response[:1000]}"
            )
            prompt = _build_extract_prompt(combined)
        else:
            prompt = _build_extract_prompt(user_message[:3000])

        # Skip immediately when the LLM channel is already busy —
        # don't queue, don't wait. The next user message will retry
        # via idempotent upsert; regex layer (high precision) has
        # already landed the URL/account/phone facts.
        sem = LLMFactExtractor._global_sem
        assert sem is not None
        if sem.locked():
            self._log.info(
                "llm_fact_extractor.skipped (channel busy)",
            )
            return []

        async with sem:
            try:
                from xmclaw.providers.llm.base import Message
                t0 = time.perf_counter()
                resp = await asyncio.wait_for(
                    self._llm.complete([Message(role="user", content=prompt)]),
                    timeout=self._timeout_s,
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
            except asyncio.TimeoutError:
                self._log.warning(
                    "llm_fact_extractor.timeout elapsed_ms=%.0f",
                    self._timeout_s * 1000.0,
                )
                return []
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "llm_fact_extractor.llm_failed err=%s", exc,
                )
                return []

        content = (getattr(resp, "content", "") or "").strip()
        # Strip markdown code-fence if model wrapped despite the
        # instruction.
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        if not content:
            return []

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            self._log.warning(
                "llm_fact_extractor.bad_json elapsed_ms=%.0f preview=%r",
                elapsed_ms, content[:200],
            )
            return []

        if not isinstance(data, list):
            return []

        facts: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            kind = item.get("kind")
            scope = item.get("scope", "project")
            confidence = item.get("confidence", 0.7)

            # Validate.
            if not isinstance(text, str) or not text.strip():
                continue
            text = text.strip()
            if len(text) > 500:
                text = text[:500]
            if kind not in _VALID_KINDS:
                continue
            if scope not in _VALID_SCOPES:
                scope = "project"
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.7
            # Clamp LLM confidence to [0.5, 0.95]. Regex hits stay
            # higher (0.78-0.95); LLM hits are inherently less sure.
            confidence = max(0.5, min(0.95, confidence))

            bucket = item.get("bucket", "")
            if not isinstance(bucket, str):
                bucket = ""
            facts.append({
                "text": text,
                "kind": kind,
                "scope": scope,
                "confidence": confidence,
                "bucket": bucket,
            })

        self._log.info(
            "llm_fact_extractor.done elapsed_ms=%.0f facts=%d",
            elapsed_ms, len(facts),
        )
        return facts

    async def extract_candidates(
        self,
        user_message: str,
        assistant_response: str | None = None,
    ) -> list[LLMCandidate]:
        """Typed variant of :meth:`extract`. Phase 7 shim #7.

        Same semantics as ``extract`` but returns a list of
        :class:`LLMCandidate` dataclasses so callers writing the V1→V2
        migration get IDE-completion + mypy on candidate fields. Use
        ``MemoryService.remember`` to persist each candidate the
        caller chooses to keep.
        """
        raw = await self.extract(user_message, assistant_response)
        return [
            LLMCandidate(
                text=d["text"],
                kind=d["kind"],
                scope=d["scope"],
                confidence=d["confidence"],
                bucket=d.get("bucket", ""),
            )
            for d in raw
        ]


# ── Convenience: extract + remember ──────────────────────────────


async def llm_extract_and_remember(
    user_message: str,
    memory_service: Any,
    llm_extractor: LLMFactExtractor,
    *,
    source_event_id: str | None = None,
) -> list[Any]:
    """Run LLM extraction + write each fact to MemoryService.

    Per-fact failures are logged but don't abort the batch — same
    posture as the regex path (idempotent upsert retries on next
    message).

    Wave-27 fix-9: facts extracted from the same user message get
    pairwise SAME_TOPIC edges. Mirrors what
    ``key_info_extractor.extract_and_remember`` does — see that
    function's docstring for the rationale (URL + credentials
    extracted from one message should NOT show up as disconnected
    nodes in the graph).
    """
    from xmclaw.memory.v2.models import RelationKind
    facts_raw = await llm_extractor.extract(user_message)
    if not facts_raw:
        return []
    written = []
    # 2026-05-28 memory v3 phase 1.4: bucket is now whatever the LLM
    # tagged it with (the extractor prompt is auto-generated from
    # the BUCKETS registry — see ``llm_extractor.py`` prompt). The
    # legacy hardcoded if/elif fallback below only triggers for old
    # extractor payloads / fact dicts that pre-date the prompt
    # update. ``MemoryService.remember`` coerces empty/unknown to
    # ``misc`` so we can never accidentally write a dark fact.
    for f in facts_raw:
        try:
            kind = f["kind"]
            scope = f["scope"]
            bucket = (f.get("bucket") or "").strip()
            if not bucket:
                # Legacy fallback for payloads from a pre-v3 extractor:
                # infer from (kind, scope). New extractor sets bucket
                # explicitly. Empty bucket here will get coerced to
                # "misc" by remember() — both paths converge.
                if kind == "identity":
                    if scope == "session":
                        bucket = "agent_identity"
                    elif scope == "user":
                        bucket = "user_identity"
                elif kind == "preference" and scope == "user":
                    bucket = "user_preference"
            fact = await memory_service.remember(
                f["text"],
                kind=kind,
                scope=scope,
                confidence=f["confidence"],
                source_event_id=source_event_id,
                bucket=bucket,
                provenance="auto_extract_llm",
            )
            written.append(fact)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "llm_fact_extractor.remember_failed text=%r err=%s",
                f["text"][:60], exc,
            )

    # Wave-27 fix-9: pairwise SAME_TOPIC co-occurrence edges.
    if len(written) >= 2:
        unique = list({f.id: f for f in written}.values())
        for i, fact_a in enumerate(unique):
            for fact_b in unique[i + 1:]:
                if fact_a.id == fact_b.id:
                    continue
                for src, dst in (
                    (fact_a.id, fact_b.id),
                    (fact_b.id, fact_a.id),
                ):
                    try:
                        await memory_service.relate(
                            source_fact_id=src,
                            target_fact_id=dst,
                            kind=RelationKind.SAME_TOPIC,
                            strength=0.80,
                            auto_extracted=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "llm_fact_extractor.cooccur_link_failed "
                            "src=%s dst=%s err=%s",
                            src[:32], dst[:32], exc,
                        )

    return written


__all__ = [
    "LLMFactExtractor",
    "llm_extract_and_remember",
]
