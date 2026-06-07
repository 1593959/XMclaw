"""Cognitive Memory Gateway — unified write/read entrypoint (Phase 1).

Phase 1 (skeleton):
  * All memory writes route through here (single point of control).
  * THINK and DECIDE are stubbed — observations passthrough to
    ``MemoryService.remember()`` (or ``remember_with_decision`` when
    enabled).  This keeps behaviour identical to pre-Gateway while
    establishing the architecture.

Phase 2 (cognitive layer):
  * THINK enabled — LLM-driven cross-turn summarisation + contradiction
    detection.
  * DECIDE enabled — Mem0-style ADD/UPDATE/DELETE/NOOP against neighbours.

Phase 3 (intelligent recall):
  * Recall gate + bucket classifier + true hybrid fusion.

Phase 4 (background consolidator):
  * Cognition's hourly consolidate_memory routes through here.

Usage (production):
    gateway = CognitiveMemoryGateway(
        memory_service=memory_v2_service,
        llm=llm_fast_tier,
        cfg=cfg.get("memory", {}).get("gateway", {}),
    )
    await gateway.ingest(Observation(...))
    recalled = await gateway.recall_for_turn("用户消息")
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from xmclaw.memory.v2.gateway_models import (
    CognitiveDigest,
    Observation,
    RecallPlan,
    RecallResult,
)
from xmclaw.memory.v2.models import Fact, FactKind, FactScope
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# ── Config defaults ──────────────────────────────────────────────


_DEFAULT_CFG: dict[str, Any] = {
    "enabled": True,
    "think": {
        "enabled": True,         # Phase 2: enabled
        "model_tier": "fast",
        "max_observations_per_batch": 5,
        "cache_ttl_s": 300,
    },
    "decide": {
        "enabled": True,         # Phase 2: enabled
        "use_remember_with_decision": True,
        "max_neighbors": 16,
    },
    "recall": {
        "gate_enabled": False,   # Phase 3: disabled until implemented
        "classify_enabled": False,
        "hybrid_enabled": True,
        "timeout_s": 3.0,
        "k": 4,
        "min_similarity": 0.72,
    },
}


# ── Gateway ──────────────────────────────────────────────────────


class CognitiveMemoryGateway:
    """Unified cognitive memory pipeline.

    Args:
        memory_service: a :class:`xmclaw.memory.v2.service.MemoryService`
            instance (required).
        llm: optional async LLM for THINK / DECIDE / recall-gate.
            When None, all LLM-dependent steps gracefully degrade to
            passthrough behaviour.
        cfg: nested dict under ``config.json → memory.gateway``.
    """

    def __init__(
        self,
        *,
        memory_service: Any,
        llm: Any | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self._svc = memory_service
        self._llm = llm
        self._cfg = _merge_cfg(_DEFAULT_CFG, cfg or {})
        self._enabled = bool(self._cfg.get("enabled", True))

        # Sub-feature flags (Phase 1 mostly False).
        _think_cfg = self._cfg.get("think", {})
        self._think_enabled = bool(_think_cfg.get("enabled", False))

        _decide_cfg = self._cfg.get("decide", {})
        self._decide_enabled = bool(_decide_cfg.get("enabled", False))
        self._use_rwd = bool(_decide_cfg.get("use_remember_with_decision", True))
        self._max_neighbors = int(_decide_cfg.get("max_neighbors", 16))

        _recall_cfg = self._cfg.get("recall", {})
        self._recall_gate = bool(_recall_cfg.get("gate_enabled", False))
        self._recall_classify = bool(_recall_cfg.get("classify_enabled", False))
        self._recall_hybrid = bool(_recall_cfg.get("hybrid_enabled", True))
        self._recall_timeout = float(_recall_cfg.get("timeout_s", 3.0))
        self._recall_k = int(_recall_cfg.get("k", 4))
        self._recall_min_sim = float(_recall_cfg.get("min_similarity", 0.72))

        # Phase 1: simple in-memory cache for THINK (prepared for Phase 2).
        self._think_cache: dict[str, tuple[float, CognitiveDigest]] = {}
        self._cache_ttl = float(_think_cfg.get("cache_ttl_s", 300))

        # Phase 5: metrics (in-memory counters, reset on restart).
        self._metrics: dict[str, Any] = {
            "ingest_total": 0,
            "ingest_dropped": 0,
            "ingest_fallback": 0,
            "ingest_actions": {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NOOP": 0},
            "think_cache_hits": 0,
            "think_cache_misses": 0,
            "recall_total": 0,
            "recall_gate_skipped": 0,
            "recall_classify_buckets": {},
            "recall_results_total": 0,
            "started_at": time.time(),
        }

    # ── Public write API ─────────────────────────────────────────

    async def ingest(
        self,
        observation: Observation,
        *,
        context: dict[str, Any] | None = None,
    ) -> Fact | None:
        """Ingest one observation through the full pipeline.

        Phase 1: THINK and DECIDE are no-ops; the observation is mapped
        directly to a ``remember()`` call.  This preserves existing
        behaviour while unifying the entrypoint.

        Returns the persisted Fact (or None when dropped / failed).
        """
        if not self._enabled or self._svc is None:
            return None

        if not observation.content or not observation.content.strip():
            return None

        self._metrics["ingest_total"] += 1

        try:
            # ── THINK (stubbed in Phase 1) ───────────────────────
            digest = await self._think(observation, context=context)
            if not digest.worth_remembering:
                self._metrics["ingest_dropped"] += 1
                _log.debug(
                    "gateway.ingest.dropped source=%s reason=%s",
                    observation.source, digest.reason,
                )
                return None

            # ── DECIDE + EXECUTE ─────────────────────────────────
            return await self._execute(digest, observation)
        except Exception as exc:  # noqa: BLE001 — never crash caller
            self._metrics["ingest_fallback"] += 1
            _log.warning(
                "gateway.ingest.failed source=%s err=%s",
                observation.source, exc,
            )
            # Fallback: best-effort blind remember so we don't lose data.
            try:
                return await self._fallback_remember(observation)
            except Exception:  # noqa: BLE001
                return None

    async def ingest_batch(
        self,
        observations: list[Observation],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[Fact | None]:
        """Batch ingest.  Preserves order; failures are logged, never raised."""
        results: list[Fact | None] = []
        for obs in observations:
            try:
                results.append(await self.ingest(obs, context=context))
            except Exception as exc:  # noqa: BLE001
                _log.warning("gateway.ingest_batch.item_failed err=%s", exc)
                results.append(None)
        return results

    # ── Public read API ──────────────────────────────────────────

    async def recall_for_turn(
        self,
        user_message: str,
        *,
        turn_context: dict[str, Any] | None = None,
    ) -> str:
        """Return a formatted recall block ready to prepend to the user
        message (or empty string when nothing relevant).

        Phase 3: intelligent recall pipeline:
          1. Gate — skip trivial turns (greetings, confirmations)
          2. Classify — keyword-driven bucket classifier
          3. Targeted hybrid recall — vector + BM25 + RRF, restricted
             to relevant buckets.
        """
        if not self._enabled or self._svc is None:
            return ""

        text = (user_message or "").strip()
        if len(text) < 4:
            return ""

        self._metrics["recall_total"] += 1
        try:
            from xmclaw.memory.v2.gateway_recall import (
                recall_for_message_via_gateway,
                render_recalled_block,
            )
            hits = await recall_for_message_via_gateway(
                gateway=self,
                user_message=text,
                k=self._recall_k,
                min_similarity=self._recall_min_sim,
                timeout_s=self._recall_timeout,
            )
            self._metrics["recall_results_total"] += len(hits)
            return render_recalled_block(hits)
        except Exception as exc:  # noqa: BLE001
            _log.warning("gateway.recall_for_turn.failed err=%s", exc)
            return ""

    async def targeted_recall(
        self,
        query: str,
        *,
        buckets: list[str] | None = None,
        k: int = 4,
        min_similarity: float = 0.72,
        timeout_s: float = 3.0,
    ) -> list[RecallResult]:
        """Targeted hybrid recall restricted to relevant buckets.

        Uses the underlying MemoryService's ``recall_hybrid`` (vector +
        BM25 + RRF) when available; falls back to plain ``recall``
        otherwise.  Results are filtered by similarity threshold and
        structural-axis bucket exclusion.

        Args:
            query: user message text.
            buckets: when non-empty, restrict recall to these buckets.
                Empty / None means unrestricted search.
            k: max results to return.
            min_similarity: cosine similarity floor [0, 1].
            timeout_s: unused (kept for API compat with legacy path).
        """
        if not self._svc:
            return []

        try:
            # Prefer recall_hybrid (vector + BM25 + RRF) when available.
            if hasattr(self._svc, "recall_hybrid"):
                raw_hits = await self._svc.recall_hybrid(
                    query,
                    k=max(k * 2, 16),
                    buckets=buckets,
                    min_confidence=0.0,
                    include_superseded=False,
                )
            else:
                raw_hits = await self._svc.recall(
                    query,
                    k=max(k * 2, 16),
                    buckets=buckets,
                    min_confidence=0.0,
                    include_relations=False,
                    include_superseded=False,
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning("gateway.targeted_recall.search_failed err=%s", exc)
            return []

        results: list[RecallResult] = []
        for h in raw_hits:
            f = h.fact
            distance = float(getattr(h, "distance", 1.0))
            similarity = max(0.0, min(1.0, 1.0 - distance))
            if similarity < min_similarity:
                continue
            bucket = (getattr(f, "bucket", "") or "").strip()
            # Skip structural-axis buckets (already in system prompt).
            if bucket in {"agent_identity", "user_identity", "user_preference", "values", "misc"}:
                continue
            results.append(RecallResult(
                fid=(getattr(f, "id", "") or "")[:12],
                text=(getattr(f, "text", "") or "").strip(),
                bucket=bucket or "misc",
                kind=(getattr(f, "kind", "") or "fact"),
                similarity=similarity,
                ts_first=float(getattr(f, "ts_first", 0.0) or 0.0),
            ))
            if len(results) >= k:
                break

        _log.debug(
            "gateway.targeted_recall query=%r buckets=%s hits=%d",
            query[:40], buckets, len(results),
        )
        return results

    # ── Internal: THINK (Phase 2) ────────────────────────────────

    async def _think(
        self,
        observation: Observation,
        *,
        context: dict[str, Any] | None = None,
    ) -> CognitiveDigest:
        """Cognitive THINK step.

        Phase 2: calls an LLM to:
          1. Summarise the observation into a compact, normalised statement
             (NOT a verbatim copy of the source text).
          2. Compare against neighbouring facts and judge whether this
             observation is worth remembering, already known, or
             contradictory.

        When the LLM is unavailable or the call fails, degrades gracefully
        to the Phase-1 passthrough behaviour.
        """
        if not self._think_enabled or self._llm is None:
            return self._passthrough_digest(observation)

        # Check cache: same content within TTL → reuse prior digest.
        cache_key = _cache_key(observation)
        cached = self._think_cache.get(cache_key)
        if cached is not None:
            ts, digest = cached
            if time.time() - ts < self._cache_ttl:
                self._metrics["think_cache_hits"] += 1
                _log.debug("gateway.think.cache_hit key=%s", cache_key[:32])
                return digest
        self._metrics["think_cache_misses"] += 1

        # 1. Fetch neighbours for context.
        neighbours = await self._fetch_neighbours(observation)

        # 2. Build prompt and call LLM.
        try:
            from xmclaw.core.ir import Message
            prompt = _build_think_prompt(observation, neighbours)
            t0 = time.perf_counter()
            resp = await self._llm.complete(
                [Message(role="user", content=prompt)],
                tools=None,
            )
            _think_latency_ms = (time.perf_counter() - t0) * 1000.0
            digest = _parse_think_response(
                resp.content or "", observation, neighbours,
            )
            _log.info(
                "gateway.think result=%s text=%r reason=%s latency_ms=%.0f",
                "keep" if digest.worth_remembering else "drop",
                digest.synthesized_text[:80],
                digest.reason[:60],
                _think_latency_ms,
            )
            # Cache the result.
            self._think_cache[cache_key] = (time.time(), digest)
            return digest
        except Exception as exc:  # noqa: BLE001
            _log.warning("gateway.think.llm_failed err=%s", exc)
            return self._passthrough_digest(observation)

    def _passthrough_digest(self, observation: Observation) -> CognitiveDigest:
        """Phase-1 fallback: map observation directly to an ADD digest."""
        md = observation.metadata
        return CognitiveDigest(
            worth_remembering=True,
            action="ADD",
            synthesized_text=observation.content.strip(),
            target_fact_id=None,
            kind=md.get("kind_hint", "lesson"),
            scope=md.get("scope_hint", "project"),
            bucket=md.get("bucket_hint", ""),
            confidence=float(md.get("confidence_hint", 0.8)),
            reason="phase1_passthrough",
        )

    async def _fetch_neighbours(
        self, observation: Observation,
    ) -> list[Any]:
        """Return up to 5 existing facts that are vector-close to the
        observation.  Used by THINK to give the LLM context for
        contradiction / duplicate detection."""
        if self._svc is None:
            return []
        try:
            hits = await self._svc.recall(
                observation.content,
                k=5,
                min_confidence=0.3,
                include_relations=False,
                include_superseded=False,
            )
            return [h.fact for h in hits]
        except Exception as exc:  # noqa: BLE001
            _log.debug("gateway.think.neighbour_fetch_failed err=%s", exc)
            return []

    # ── Internal: DECIDE + EXECUTE ───────────────────────────────

    async def _execute(
        self,
        digest: CognitiveDigest,
        observation: Observation,
    ) -> Fact | None:
        """Execute the digest's action against the store.

        Phase 1: always ADD via ``remember()`` (or
        ``remember_with_decision`` when configured and the service
        exposes it).
        """
        text = digest.synthesized_text or observation.content
        kind = digest.kind
        scope = digest.scope
        bucket = digest.bucket
        confidence = digest.confidence

        # Phase 1: when decide is disabled, route through
        # remember_with_decision if the service has it AND the user
        # configured it.  This wires up the existing Mem0 decision
        # layer that was previously悬空.
        if (
            self._use_rwd
            and hasattr(self._svc, "remember_with_decision")
        ):
            try:
                result = await self._svc.remember_with_decision(
                    text=text,
                    kind=kind,
                    scope=scope,
                    confidence=confidence,
                    source_event_id=observation.turn_id,
                )
                fact = result.get("fact")
                action = result.get("action", "ADD")
                self._metrics["ingest_actions"][action] = (
                    self._metrics["ingest_actions"].get(action, 0) + 1
                )
                if fact is not None:
                    _log.info(
                        "gateway.execute.rwd action=%s fact_id=%s",
                        action, fact.id[:16],
                    )
                return fact
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "gateway.execute.rwd_failed err=%s — falling back to "
                    "plain remember()", exc,
                )
                # Fall through to plain remember.

        # Plain remember (default / fallback).
        self._metrics["ingest_actions"]["ADD"] += 1
        return await self._svc.remember(
            text,
            kind=kind,
            scope=scope,
            confidence=confidence,
            source_event_id=observation.turn_id,
            bucket=bucket,
        )

    async def _fallback_remember(self, observation: Observation) -> Fact | None:
        """Emergency fallback when the main pipeline throws."""
        return await self._svc.remember(
            observation.content,
            kind=observation.metadata.get("kind_hint", "lesson"),
            scope=observation.metadata.get("scope_hint", "project"),
            confidence=0.7,
            source_event_id=observation.turn_id,
            bucket=observation.metadata.get("bucket_hint", ""),
        )

    # ── Properties (read-only) ───────────────────────────────────

    @property
    def memory_service(self) -> Any:
        return self._svc

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_metrics(self) -> dict[str, Any]:
        """Return a snapshot of Gateway metrics."""
        m = dict(self._metrics)
        m["uptime_s"] = round(time.time() - m["started_at"], 2)
        return m


# ── THINK prompt / parse helpers ───────────────────────────────


_THINK_PROMPT_TEMPLATE = """\
你是记忆系统的「认知层」。你的任务是从用户的对话片段中提取有价值的长期记忆，但必须做真正的「总结归纳」，而不是原文摘抄。

【规则】
1. **必须总结归纳**：禁止原文复述。把用户的原话提炼成一句简洁、规范的事实陈述。例如：
   - 用户说「我那个网店是 pw310 的」→ 归纳后：「用户运营的网店域名为 pw310」
   - 用户说「以后都用中文跟我聊」→ 归纳后：「用户偏好使用中文进行交流」
2. **区分稳定事实 vs 临时命令**：
   - ✅ 稳定事实：跨会话仍成立的信息（偏好、身份、长期目标、已做决定、技术栈）
   - ❌ 临时命令：只针对当前回答的动作请求（"帮我改配置"、"删除X"、"运行Y"）
3. **判断与已有记忆的关系**：
   - 如果已有记忆已完全覆盖这条信息 → worth_remembering = false
   - 如果新信息补充了已有记忆 → worth_remembering = true
   - 如果新信息与已有记忆矛盾 → worth_remembering = true（系统会自动处理矛盾）
4. **输出要求**：只返回纯 JSON，不要 markdown 代码块，不要任何解释文字。

【当前观察】
来源: {source}
内容: {content}

【已有相关记忆】{neighbours_block}

【输出格式】
{{
  "worth_remembering": true/false,
  "synthesized_text": "归纳后的规范陈述句（一句话，中文）",
  "reason": "简要说明判断理由"
}}
"""


def _build_think_prompt(
    observation: Observation,
    neighbours: list[Any],
) -> str:
    """Build the THINK prompt with observation + neighbour context."""
    if neighbours:
        lines = []
        for i, nb in enumerate(neighbours, 1):
            text = getattr(nb, "text", "") or ""
            kind = getattr(nb, "kind", "") or ""
            lines.append(f"  {i}. [{kind}] {text}")
        neighbours_block = "\n" + "\n".join(lines)
    else:
        neighbours_block = "\n  （无相关记忆）"

    return _THINK_PROMPT_TEMPLATE.format(
        source=observation.source,
        content=observation.content,
        neighbours_block=neighbours_block,
    )


def _parse_think_response(
    raw: str,
    observation: Observation,
    neighbours: list[Any],
) -> CognitiveDigest:
    """Parse the LLM's THINK response into a CognitiveDigest.

    Defensive: any parse failure → passthrough digest so we never
    silently drop a fact due to JSON malformation.
    """
    import json as _json

    text = (raw or "").strip()
    if not text:
        return _passthrough_digest_from_obs(observation)

    # Strip markdown fences if the model wrapped despite instruction.
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```").strip()

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        # Try to extract the first JSON object from the text.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = _json.loads(text[start:end + 1])
            except _json.JSONDecodeError:
                return _passthrough_digest_from_obs(observation)
        else:
            return _passthrough_digest_from_obs(observation)

    if not isinstance(data, dict):
        return _passthrough_digest_from_obs(observation)

    worth = bool(data.get("worth_remembering", True))
    synthesized = str(data.get("synthesized_text") or "").strip()
    if not synthesized:
        # Fallback to original content if LLM returned empty text.
        synthesized = observation.content.strip()

    md = observation.metadata
    return CognitiveDigest(
        worth_remembering=worth,
        action="ADD",  # DECIDE layer (remember_with_decision) resolves this.
        synthesized_text=synthesized,
        target_fact_id=None,
        kind=md.get("kind_hint", "lesson"),
        scope=md.get("scope_hint", "project"),
        bucket=md.get("bucket_hint", ""),
        confidence=float(md.get("confidence_hint", 0.8)),
        reason=str(data.get("reason") or "think_phase2"),
    )


def _passthrough_digest_from_obs(observation: Observation) -> CognitiveDigest:
    """Fallback digest when THINK parsing fails."""
    md = observation.metadata
    return CognitiveDigest(
        worth_remembering=True,
        action="ADD",
        synthesized_text=observation.content.strip(),
        target_fact_id=None,
        kind=md.get("kind_hint", "lesson"),
        scope=md.get("scope_hint", "project"),
        bucket=md.get("bucket_hint", ""),
        confidence=float(md.get("confidence_hint", 0.8)),
        reason="think_parse_fallback",
    )


def _cache_key(observation: Observation) -> str:
    """Deterministic cache key for a THINK result.

    Based on (source, content, kind_hint, scope_hint) so identical
    observations within the TTL reuse the digest without a second
    LLM call."""
    import hashlib
    md = observation.metadata
    payload = (
        f"{observation.source}\x00"
        f"{observation.content}\x00"
        f"{md.get('kind_hint', '')}\x00"
        f"{md.get('scope_hint', '')}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ── Helpers ──────────────────────────────────────────────────────


def _merge_cfg(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``override`` into ``base``.  Shallow copies only
    top-level keys; nested dicts are merged recursively."""
    out = dict(base)
    for key, val in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(val, dict)
        ):
            out[key] = _merge_cfg(out[key], val)
        else:
            out[key] = val
    return out
