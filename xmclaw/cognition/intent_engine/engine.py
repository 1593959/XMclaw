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
_CONFIDENCE_THRESHOLD = 0.55
# How often Layer-2 statistical pass runs (seconds).
_STATISTICAL_INTERVAL_S = 60.0
# How often Layer-3 LLM pass runs (seconds).
_LLM_INTERVAL_S = 300.0


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
            "You are an intent-prediction analyst. Given a compressed log of "
            "recent user-agent interactions, predict the user's MOST LIKELY "
            "next intent(s) within the next few minutes.\n\n"
            "Respond with a single JSON object (no markdown fences):\n"
            '{"predictions": [{"intent_type": "snake_case_slug", '
            '"confidence": 0.0-1.0, "rationale": "brief explanation", '
            '"proposed_message": "proactive suggestion text", '
            '"urgency": "low|normal|high"}]}\n\n'
            "Only emit predictions with confidence >= 0.55. "
            "If nothing is clearly predictable, return an empty predictions array."
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
            pred = IntentPrediction(
                intent_type=intent_type,
                confidence=round(conf, 3),
                rationale=str(item.get("rationale", "")),
                proposed_action={
                    "message": str(item.get("proposed_message", "")),
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
