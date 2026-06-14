"""IntentEngine — three-layer predictive intent detection.

Layer 1 (Rule):    O(1) heuristics per event — cheap, explainable.
Layer 2 (Statistical):  periodic EWMA + association rules over the
                   sliding context window — medium cost.
Layer 3 (LLM):     low-frequency LLM reasoning over the past 24 h
                   of events — expensive, high quality.

All layers emit :class:`IntentPrediction` objects. The
:meth:`top_predictions` API surfaces the highest-confidence items
for the :class:`IntentPredictionTrigger` to consume.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from xmclaw.core.bus.events import BehavioralEvent
from xmclaw.core.ir import Message
from xmclaw.cognition.intent_engine.models import IntentPrediction, ProactiveProposal, UserPattern
from xmclaw.cognition.intent_engine.store import IntentStore
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# Human-readable labels for intent slugs surfaced to users.
# Keep in sync with _register_builtin_rules() intent_type values.
_INTENT_LABELS: dict[str, str] = {
    "review_recent_failures": "复盘最近的执行失败",
    "post_deploy_check": "部署后的检查",
    "tune_memory_retention": "记忆保留策略调优",
}

# Maximum events kept in the hot context window.
_MAX_CONTEXT_WINDOW = 256
# Minimum confidence before a prediction is surfaced to triggers.
_CONFIDENCE_THRESHOLD = 0.65
# How often Layer-2 statistical pass runs (seconds).
_STATISTICAL_INTERVAL_S = 60.0
# How often Layer-3 LLM pass runs (seconds).
_LLM_INTERVAL_S = 600.0


# 2026-05-29 honesty guard. A proactive prediction is just a string
# shown to the user — no code runs behind it. So a message that
# claims (first person) the agent is doing / has done / will do work
# is a lie. These patterns catch the common Chinese phrasings; the
# proper framing is a question/offer ("要不要我帮你…？"), which none
# of these match. Kept intentionally tight to avoid nuking honest
# offers — only present-progressive / completed / future-commitment
# self-claims trip it.
import re as _re

_FALSE_ACTION_CLAIM_RE = _re.compile(
    r"我(?:已经|正在|现在|刚刚|马上|这就)?"
    r"(?:在)?"
    r"(?:处理|分析|整理|去重|合并|清理|执行|运行|生成|"
    r"帮你|给你|完成|搞定|做完|跑完|算完)"
    r"|处理完(?:后|了)?我(?:会|将|来|给)"
    r"|我会(?:把|给|帮|去|来|开始)"
    r"|稍后(?:我|给你)"
)

# Offer / question framing — when present, a "我帮你…" is an honest
# OFFER ("要不要我帮你去重？"), not a false claim of in-progress
# work. We exempt these so the guard doesn't nuke legitimate
# proactive offers.
_OFFER_MARKER_RE = _re.compile(
    r"要不要|需不需要|想不想|是否(?:需要|要)|需要的话|"
    r"要(?:我|不要我)|我可以(?:帮|给|为)|"
    r"如果(?:你)?(?:需要|愿意|想)|"
    r"[?？]"
)


def _claims_false_action(message: str) -> bool:
    """True if ``message`` makes a first-person claim of doing /
    having done / committing-to-do work. Proactive proposals carry
    no execution, so such a claim misleads the user.

    An offer/question framing (``要不要我帮你…？``) is honest by
    construction — it proposes, doesn't claim — so those are
    exempt even when they contain a work verb."""
    if not message:
        return False
    if _OFFER_MARKER_RE.search(message):
        return False
    return bool(_FALSE_ACTION_CLAIM_RE.search(message))


@dataclass
class _RuleLayer:
    """Fast heuristic layer — evaluates every incoming event immediately."""

    # (event_type, payload_key_predicate) -> IntentPrediction factory
    _handlers: dict[str, Callable[[dict[str, Any]], IntentPrediction | None]] = field(
        default_factory=dict,
    )

    def register(
        self,
        event_type: str,
        factory: Callable[[dict[str, Any]], IntentPrediction | None],
    ) -> None:
        self._handlers[event_type] = factory

    def evaluate(self, event: dict[str, Any]) -> IntentPrediction | None:
        ev_type = event.get("type", "")
        factory = self._handlers.get(ev_type)
        if factory is None:
            return None
        try:
            return factory(event.get("payload", {}))
        except Exception as exc:  # noqa: BLE001
            _log.debug("rule_layer.evaluate_failed: %s", exc)
            return None


class IntentEngine:
    """Main orchestrator for intent prediction.

    Usage::

        engine = IntentEngine(store=IntentStore(db_path), bus_subscribe=bus.subscribe)
        # EventBus feeds events:
        await engine.on_event(behavioral_event_dict)
        # Proactive trigger queries:
        predictions = engine.top_predictions(k=3)
    """

    def __init__(
        self,
        store: IntentStore,
        *,
        bus_subscribe: Callable[[Callable[[Any], Awaitable[None]]], Any] | None = None,
        llm: Any | None = None,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD,
    ) -> None:
        self._store = store
        self._llm = llm
        self._confidence_threshold = confidence_threshold

        # Hot sliding window of recent events (dicts with type, payload, ts).
        self._context_window: deque[dict[str, Any]] = deque(maxlen=_MAX_CONTEXT_WINDOW)
        # Timestamps of last Layer-2 / Layer-3 runs.
        self._last_statistical_ts: float = 0.0
        self._last_llm_ts: float = 0.0

        # Rule layer with built-in heuristics.
        self._rule_layer = _RuleLayer()
        self._register_builtin_rules()

        # Latest predictions cache (refreshed by layers).
        self._prediction_cache: list[IntentPrediction] = []
        self._cache_ts: float = 0.0

        if bus_subscribe is not None:
            # IntentEngine itself is NOT async in __init__, so we store the
            # callback for the caller to wire later (lifespan does it).
            self._bus_subscribe = bus_subscribe

    # ── public API ──

    async def on_event(self, event: BehavioralEvent) -> None:
        """Ingest one BehavioralEvent. Called by EventBus subscriber."""
        _ts = float(event.ts)
        _type = event.type.value if hasattr(event.type, "value") else str(event.type)
        flat: dict[str, Any] = {
            "type": _type,
            "payload": dict(event.payload),
            "ts": _ts,
        }
        self._context_window.append(flat)

        # Layer 1 — immediate rule evaluation.
        if pred := self._rule_layer.evaluate(flat):
            _log.debug(
                "intent_engine.rule_hit: %s confidence=%.2f",
                pred.intent_type, pred.confidence,
            )
            self._prediction_cache.append(pred)
            self._cache_ts = _ts

        # Persist antecedent → intent observation (learn from every event).
        await self._learn_from_event(flat)

        # Layer 2 — periodic statistical pass.
        if _ts - self._last_statistical_ts >= _STATISTICAL_INTERVAL_S:
            self._last_statistical_ts = _ts
            await self._run_statistical_layer()

        # Layer 3 — periodic LLM pass (if wired).
        if self._llm is not None and _ts - self._last_llm_ts >= _LLM_INTERVAL_S:
            self._last_llm_ts = _ts
            await self._run_llm_layer()

    def top_predictions(
        self,
        *,
        k: int = 3,
        min_confidence: float | None = None,
    ) -> list[IntentPrediction]:
        """Return highest-confidence predictions for the current tick.

        Called by :class:`IntentPredictionTrigger` inside the
        ProactiveAgent tick loop.
        """
        threshold = min_confidence if min_confidence is not None else self._confidence_threshold
        # Deduplicate by intent_type, keep highest confidence.
        by_intent: dict[str, IntentPrediction] = {}
        for pred in self._prediction_cache:
            if pred.confidence < threshold:
                continue
            existing = by_intent.get(pred.intent_type)
            if existing is None or pred.confidence > existing.confidence:
                by_intent[pred.intent_type] = pred

        # Sort descending by confidence.
        sorted_preds = sorted(by_intent.values(), key=lambda p: p.confidence, reverse=True)
        return sorted_preds[:k]

    def to_proposal(self, prediction: IntentPrediction) -> ProactiveProposal:
        """Convert a prediction into a concrete proposal for the bus."""
        action = prediction.proposed_action
        return ProactiveProposal(
            message=action.get("message", prediction.rationale),
            urgency=action.get("urgency", "normal"),
            confidence=prediction.confidence,
            intent_type=prediction.intent_type,
            payload={
                "pattern_id": prediction.pattern_id,
                "source_layer": prediction.source_layer,
                **action,
            },
        )

    def record_user_reaction(
        self,
        pattern_id: str | None,
        reaction: str,  # accepted | ignored | dismissed | snoozed
    ) -> None:
        """Close the feedback loop. Called when the user acts on (or ignores)
        a proactive proposal surfaced by this engine."""
        self._store.record_feedback(pattern_id, reaction)
        if pattern_id and reaction in ("accepted", "dismissed"):
            # Bayesian-ish confidence update: accepted → +0.05, dismissed → -0.10
            stats = self._store.feedback_stats(pattern_id)
            total = sum(stats.values())
            if total > 0:
                acc = stats.get("accepted", 0)
                new_conf = acc / total
                self._store.update_confidence(pattern_id, new_conf)

    # ── layer implementations ──

    def _register_builtin_rules(self) -> None:
        """Register out-of-the-box rule heuristics."""

        # Rule: high grader failure streak → suggest review.
        def _grader_fail(payload: dict[str, Any]) -> IntentPrediction | None:
            verdict = payload.get("verdict", "")
            if verdict != "fail":
                return None
            score = payload.get("deterministic_score") or payload.get("score") or 0.0
            if isinstance(score, (int, float)) and score < 0.3:
                return IntentPrediction(
                    intent_type="review_recent_failures",
                    confidence=0.7,
                    rationale="最近连续出现低分判定，建议复盘。",
                    proposed_action={
                        "message": "刚才有几个执行结果评分偏低，要我帮你复盘一下问题吗？",
                        "urgency": "low",
                    },
                    source_layer="rule",
                )
            return None

        self._rule_layer.register("grader_verdict", _grader_fail)

        # Rule: user mentions a deployment-related keyword.
        def _user_deploy(payload: dict[str, Any]) -> IntentPrediction | None:
            text = str(payload.get("text", "")).lower()
            triggers = ("deploy", "部署", "上线", "release", "publish")
            if any(t in text for t in triggers):
                return IntentPrediction(
                    intent_type="post_deploy_check",
                    confidence=0.6,
                    rationale="用户提到部署，预测接下来会查日志或监控。",
                    proposed_action={
                        "message": "准备部署？我可以先帮你跑一遍测试并检查最近的变更。",
                        "urgency": "normal",
                    },
                    source_layer="rule",
                )
            return None

        self._rule_layer.register("user_message", _user_deploy)

        # Rule: memory eviction with high count → suggest compaction tuning.
        def _memory_evict(payload: dict[str, Any]) -> IntentPrediction | None:
            count = payload.get("count", 0)
            if isinstance(count, int) and count > 500:
                return IntentPrediction(
                    intent_type="tune_memory_retention",
                    confidence=0.55,
                    rationale="大量记忆条目被驱逐，可能是保留策略太激进。",
                    proposed_action={
                        "message": f"记忆存储刚刚批量清理了 {count} 条记录，要不要我帮你调一下保留策略？",
                        "urgency": "low",
                    },
                    source_layer="rule",
                )
            return None

        self._rule_layer.register("memory_evicted", _memory_evict)

    async def _learn_from_event(self, event: dict[str, Any]) -> None:
        """Update pattern frequencies from the hot window."""
        # Simple bigram: last two event types → predict current event type.
        if len(self._context_window) < 2:
            return
        prev = list(self._context_window)[-2]
        antecedent = [prev["type"], event["type"]]
        pattern_id = self._hash_antecedent(antecedent)
        existing = self._store.get_pattern(pattern_id)
        if existing is not None:
            self._store.bump_frequency(pattern_id)
        else:
            pattern = UserPattern(
                pattern_id=pattern_id,
                label=f"{' → '.join(antecedent)}",
                antecedent=antecedent,
                predicted_intent=event["type"],
                frequency=1,
                confidence=0.5,
                last_seen=time.time(),
                context_buckets=self._current_context_buckets(),
            )
            self._store.upsert_pattern(pattern)

    async def _run_statistical_layer(self) -> None:
        """Layer 2: scan stored patterns that match current context buckets
        and promote them into the prediction cache if confidence is high.
        """
        buckets = self._current_context_buckets()
        # Query patterns that share at least one context bucket.
        candidates = self._store.list_patterns(min_confidence=0.3, limit=50)
        for pat in candidates:
            match_score = self._bucket_match_score(pat.context_buckets, buckets)
            if match_score <= 0:
                continue
            # Confidence decays with age (half-life 24 h).
            age_hours = (time.time() - pat.last_seen) / 3600.0
            decay = 0.5 ** (age_hours / 24.0)
            effective_conf = pat.confidence * decay * match_score
            if effective_conf < self._confidence_threshold:
                continue
            label = _INTENT_LABELS.get(pat.predicted_intent, pat.predicted_intent)
            pred = IntentPrediction(
                intent_type=pat.predicted_intent,
                confidence=round(effective_conf, 3),
                rationale=f"历史模式「{pat.label}」匹配当前上下文",
                pattern_id=pat.pattern_id,
                proposed_action={
                    "message": f"根据你的习惯，接下来可能要处理「{label}」。需要我提前准备吗？",
                    "urgency": "low",
                },
                source_layer="statistical",
            )
            self._prediction_cache.append(pred)
            self._cache_ts = time.time()
        # Trim cache to avoid unbounded growth.
        self._trim_cache()

    async def _run_llm_layer(self) -> None:
        """Layer 3: ask the LLM to reason over the past N events.

        Compresses the recent event window into a concise summary,
        asks the LLM to predict the user's next likely intent,
        and surfaces high-confidence predictions into the cache.
        """
        if self._llm is None:
            return

        # Sample the most recent events (cap to keep prompt small).
        recent = list(self._context_window)[-32:]
        if not recent:
            return

        summary = self._compress_events(recent)

        system_prompt = (
            "你是一位意图预测分析师。根据下方压缩后的用户-代理交互日志，"
            "预测用户接下来几分钟内最可能产生的意图。\n\n"
            "请返回一个纯 JSON 对象（不要 markdown 代码块）：\n"
            '{"predictions": [{"intent_type": "snake_case_slug", '
            '"confidence": 0.0-1.0, "rationale": "简要说明", '
            '"proposed_message": "主动建议的文案", '
            '"urgency": "low|normal|high"}]}\n\n'
            "规则：\n"
            "1. 所有文本字段（rationale、proposed_message）必须使用简体中文。\n"
            "2. 只有置信度 >= 0.65 的预测才放入 predictions 数组。\n"
            "3. **大多数时候应该返回空数组 {\"predictions\": []}**。主动提议是稀缺资源，"
            "不是每个操作完成后都要刷存在感。一天出现 1-3 次是合理频率，多了就是骚扰。\n"
            "4. proposed_message 要自然、口语化。\n\n"
            "★ 低价值提议黑名单（以下场景禁止生成提议，置信度直接判 0）：\n"
            "  • 某个操作刚刚完成（'刚索引完'/'刚写完'/'刚整理完'/'刚分析完'等）→ "
            "用户不需要你跟在屁股后面问要不要搜、要不要看。如果真的有价值，"
            "用户自己会问。\n"
            "  • 没有具体 action 的寒暄（'今天搞了啥'/'要不要过一遍'等）→ 纯骚扰。\n"
            "  • 信息量等于零的泛泛提议（'我注意到你…，需要的话我可以…'而没有说 "
            "具体做什么）→ 等价于没说。\n\n"
            "★ 高价值提议白名单（以下场景才值得举手）：\n"
            "  • 连续多次失败 → 建议切换策略或排查根因\n"
            "  • 发现真正异常的模式（磁盘满了、API 错误率飙升、大量记忆被驱逐）\n"
            "  • 长期沉默后的首次活动 → 可以简要同步状态\n"
            "  • 用户明确设置了提醒/定时任务即将触发\n\n"
            "★ 诚实性硬规则（最重要）：proposed_message 只是一条**主动提议**，"
            "发出后**没有任何实际动作会被执行**——它只是显示给用户看的一句话。\n"
            "  • **禁止**用第一人称声称你正在做或已经做了任何工作。"
            "不允许出现'我已经在处理…'/'我正在…'/'我已经帮你…'/"
            "'处理完后我会给你…'/'我会去做…'这类表述——这是撒谎，"
            "因为背后根本没有代码在跑。\n"
            "  • 正确写法是**询问式的提议**：'要不要我帮你…？'/"
            "'我注意到…，需要的话我可以…'/'看起来你在…，是否需要我…？'。\n"
            "  • 把它想成：你只是在**举手提议**，等用户点头你才会真正动手。"
        )

        try:
            resp = await self._llm.complete(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=summary),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("intent_engine.llm_layer.failed: %s", exc)
            return

        text = resp.content.strip()
        if not text:
            return

        # Strip markdown fences if the model wrapped JSON.
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```")
            text = text.removesuffix("```").strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            _log.debug("intent_engine.llm_layer.bad_json: %r", text[:200])
            return

        predictions = parsed.get("predictions")
        if not isinstance(predictions, list):
            return

        added = 0
        for item in predictions:
            if not isinstance(item, dict):
                continue
            conf = float(item.get("confidence", 0))
            if conf < self._confidence_threshold:
                continue
            intent_type = str(item.get("intent_type", "")).strip()
            if not intent_type:
                continue
            message = str(item.get("proposed_message", ""))
            # 2026-05-29 honesty guard (chat report): even with the
            # prompt rule, the LLM occasionally emits a first-person
            # work-claim ("我已经在处理…我会给你合并摘要") for a
            # proactive proposal that runs NO actual code. Such a
            # message lies to the user. Drop the prediction rather
            # than surface a false claim of action.
            if _claims_false_action(message):
                _log.info(
                    "intent_engine.dropped_false_action_claim msg=%r",
                    message[:120],
                )
                continue
            pred = IntentPrediction(
                intent_type=intent_type,
                confidence=round(conf, 3),
                rationale=str(item.get("rationale", "")),
                proposed_action={
                    "message": message,
                    "urgency": str(item.get("urgency", "normal")),
                },
                source_layer="llm",
            )
            self._prediction_cache.append(pred)
            added += 1

        if added:
            self._cache_ts = time.time()
            _log.info("intent_engine.llm_layer.hit: count=%d", added)
        else:
            _log.debug("intent_engine.llm_layer.no_predictions")

        self._trim_cache()

    def _compress_events(self, events: list[dict[str, Any]]) -> str:
        """Compress a list of events into a concise text summary for the LLM."""
        lines: list[str] = []
        for ev in events:
            ts = ev.get("ts", 0)
            time_str = time.strftime("%H:%M", time.localtime(ts))
            ev_type = ev.get("type", "unknown")
            payload = ev.get("payload", {})
            # Extract one key piece of info per event type to keep it short.
            if ev_type == "user_message":
                text = str(payload.get("content", ""))[:60]
                lines.append(f"[{time_str}] user: {text}")
            elif ev_type == "grader_verdict":
                verdict = payload.get("verdict", "?")
                lines.append(f"[{time_str}] grader: {verdict}")
            elif ev_type == "tool_invocation_finished":
                name = payload.get("tool_name", "?")
                ok = "ok" if not payload.get("error") else "fail"
                lines.append(f"[{time_str}] tool {name}: {ok}")
            elif ev_type == "llm_chunk":
                continue  # Skip streaming chunks — too noisy.
            else:
                # Generic fallback.
                lines.append(f"[{time_str}] {ev_type}")
        return "Recent events (newest last):\n" + "\n".join(lines)

    # ── helpers ──

    @staticmethod
    def _hash_antecedent(antecedent: list[str]) -> str:
        payload = json.dumps(antecedent, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _current_context_buckets(self) -> dict[str, str]:
        """Return coarse context tags for the current moment."""
        lt = time.localtime()
        return {
            "hour_bucket": f"{lt.tm_hour // 4 * 4:02d}",  # 00, 04, 08, 12, 16, 20
            "weekday": str(lt.tm_wday),
            "session_count": str(len(self._context_window)),
        }

    @staticmethod
    def _bucket_match_score(pat_buckets: dict[str, str], current: dict[str, str]) -> float:
        if not pat_buckets or not current:
            return 0.0
        keys = set(pat_buckets.keys()) & set(current.keys())
        if not keys:
            return 0.0
        hits = sum(1 for k in keys if pat_buckets[k] == current[k])
        return hits / len(keys)

    def _trim_cache(self) -> None:
        """Keep only the most recent 128 predictions to avoid unbounded growth."""
        if len(self._prediction_cache) > 128:
            self._prediction_cache = self._prediction_cache[-128:]
