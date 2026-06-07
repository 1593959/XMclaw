"""Gateway recall — intelligent read path (Phase 3).

Three-layer intelligent recall:
  1. GATE     — heuristic filter: skip greetings / confirmations / short msgs
  2. CLASSIFY — semantic bucket classifier (embedding-based, with keyword fallback)
  3. SEARCH   — targeted hybrid recall (vector + BM25 + RRF) restricted to relevant buckets

When no buckets are matched, falls back to unrestricted hybrid search
(catches facts that don't fit obvious keyword patterns).
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from xmclaw.memory.v2.gateway_models import RecallResult
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


def _inc_metric(
    gateway: Any,
    key: str,
    sub_key: str | None = None,
) -> None:
    """Best-effort increment of a Gateway metric counter."""
    try:
        metrics = getattr(gateway, "_metrics", None)
        if metrics is None:
            return
        if sub_key is not None:
            metrics.setdefault(key, {})
            metrics[key][sub_key] = metrics[key].get(sub_key, 0) + 1
        else:
            metrics[key] = metrics.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        pass


# ── Layer 1: GATE (heuristic) ────────────────────────────────────


_SHOULD_RECALL_MIN_CHARS: int = 8

# Greetings / small-talk — never need memory.
_SHOULD_RECALL_GREETINGS: frozenset[str] = frozenset({
    "你好", "您好", "hi", "hello", "hey", "在吗", "在么",
    "早上好", "晚上好", "下午好", "哈喽", "hola",
})

# Simple confirmations / acknowledgements — never need memory.
_SHOULD_RECALL_CONFIRMATIONS: frozenset[str] = frozenset({
    "好的", "ok", "okay", "没问题", "可以", "行", "嗯", "哦",
    "知道了", "明白了", "谢谢", "多谢", "辛苦了", "okok", "okk",
    "是的", "对的", "没错", "好", "嗯嗯", "oo", "o",
})


def should_recall_heuristic(user_message: str) -> bool:
    """Lightweight gate: should we even try to recall for this message?

    Returns False for:
      * extremely short messages (< 8 chars)
      * greetings / small-talk
      * simple confirmations
      * single emoji / punctuation-only

    This is deliberately conservative — better to miss a marginal
    recall than to inject noise into every turn.
    """
    text = (user_message or "").strip()
    if len(text) < _SHOULD_RECALL_MIN_CHARS:
        return False

    # Strip common punctuation for classification.
    stripped = text.strip("。！？.!?~… ")
    lower = stripped.lower()

    if lower in _SHOULD_RECALL_GREETINGS:
        return False
    if lower in _SHOULD_RECALL_CONFIRMATIONS:
        return False

    # Punctuation-only / emoji-only after stripping.
    if not any("一" <= ch <= "龥" or ch.isalpha() or ch.isdigit() for ch in stripped):
        return False

    return True


# ── Layer 2: CLASSIFY (keyword-driven bucket classifier) ─────────


# Keywords → bucket mapping.  A message may match multiple buckets;
# the targeted recall searches the union.
_BUCKET_KEYWORDS: dict[str, frozenset[str]] = {
    "project_fact": frozenset({
        "项目", "网站", "域名", "网址", "店铺", "网店", "后台",
        "账号", "密码", "用户名", "api", "url", "链接", "地址",
        "目标", "流水", "gmv", "营收", "订单", "客户", "用户",
        "数据库", "服务器", "部署", "发布", "版本",
    }),
    "workflow": frozenset({
        "流程", "步骤", "怎么", "如何", "怎样", "做法", "方法",
        "操作", "教程", "指南", "文档", "规范", "最佳实践",
    }),
    "tool_quirks": frozenset({
        "工具", "命令", "脚本", "配置", "参数", "选项",
        "bug", "错误", "失败", "报错", "异常", "问题", "故障",
        "崩溃", "卡住", "超时", "无法运行", "不能用",
    }),
    "failure_modes": frozenset({
        "失败", "报错", "异常", "崩溃", "卡住", "超时",
        "无法", "不能", "不行", "没反应", "没结果", "报错信息",
    }),
    "user_preference": frozenset({
        "喜欢", "偏好", "习惯", "想要", "希望", "需要",
        "不要", "别", "永远", "总是", "从不", "禁止",
        "用中文", "用英文", "简洁", "详细", "详细点",
    }),
    "user_identity": frozenset({
        "我是", "我叫", "我的名字", "我们做", "我公司", "我们团队",
        "行业", "业务", "职业", "工作", "职位", "角色",
    }),
    "rules": frozenset({
        "规则", "约束", "限制", "必须", "务必", "一定",
        "禁止", "不能", "不准", "不可", "别", "不要",
        "永远别", "再也不", "绝对",
    }),
    "values": frozenset({
        "价值观", "原则", "理念", "文化", "信条", "信念",
        "追求", "使命", "愿景",
    }),
}

# Buckets that are already injected via the structural axis (.md files).
# The similarity axis skips them to avoid double-injection.
_STRUCTURAL_BUCKETS: frozenset[str] = frozenset({
    "agent_identity", "user_identity", "user_preference", "values", "misc",
})


# ── Layer 2b: SEMANTIC CLASSIFY (embedding-based) ──────────────

# Bucket semantic descriptions (Chinese, for multilingual embedders).
# These capture the *meaning* of each bucket, not just keywords.
_BUCKET_DESCRIPTIONS: dict[str, str] = {
    "project_fact": "关于项目、网站、域名、账号密码、技术栈、业务目标、数据库、服务器、部署发布、版本控制、店铺运营、流水营收等具体业务信息",
    "workflow": "关于操作流程、步骤顺序、怎么做、方法教程、指南文档、规范标准、最佳实践、工作方式",
    "tool_quirks": "关于工具使用、命令行、脚本、配置参数、选项设置、软件行为、工具特性、环境搭建",
    "failure_modes": "关于失败、报错、异常、崩溃、卡住、超时、无法运行、错误排查、问题诊断、故障处理",
    "user_preference": "关于用户喜欢什么、偏好、习惯、想要什么、风格选择、语言选择、不要什么、简洁或详细",
    "user_identity": "关于用户是谁、名字、公司、团队、行业、职业、角色、业务领域、个人背景",
    "rules": "关于规则、约束、限制、必须做什么、禁止做什么、规范要求、硬性规定、底线",
    "values": "关于价值观、原则、理念、文化、信念、追求、使命、愿景、精神内核",
}


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


async def classify_buckets_semantic(
    user_message: str,
    embedder: Any,
    threshold: float = 0.50,
    top_k: int = 3,
) -> list[str]:
    """Embedding-based semantic bucket classifier.

    Compares the user's message against pre-defined semantic descriptions
    of each bucket using cosine similarity. Returns top-k buckets whose
    similarity exceeds the threshold.

    Falls back to empty list if embedder is unavailable or fails.
    """
    if embedder is None:
        return []

    text = (user_message or "").strip()
    if len(text) < 4:
        return []

    try:
        # Embed the user message.
        qvec = tuple(await embedder.embed(text))

        # Embed bucket descriptions (cached by embedder's LRU).
        descs = list(_BUCKET_DESCRIPTIONS.items())
        desc_texts = [d for _, d in descs]
        desc_vectors = await embedder.embed_batch(desc_texts)

        scores: list[tuple[str, float]] = []
        for (bucket, _), dvec in zip(descs, desc_vectors):
            sim = _cosine_similarity(qvec, dvec)
            scores.append((bucket, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [b for b, s in scores[:top_k] if s >= threshold]
    except Exception as exc:  # noqa: BLE001
        _log.debug("classify.semantic_failed err=%s", exc)
        return []


def classify_buckets_heuristic(user_message: str) -> list[str]:
    """Keyword-driven bucket classifier (fallback when no embedder).

    Returns a list of relevant bucket names.  Empty list means
    "no strong signal — search everything" (fallback to unrestricted
    hybrid recall).

    The classifier is intentionally permissive: a message may match
    multiple buckets, and the recall searches their union.
    """
    text = (user_message or "").lower()
    matched: list[str] = []
    for bucket, keywords in _BUCKET_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            matched.append(bucket)
    return matched


# ── Layer 3: SEARCH (targeted hybrid recall) ─────────────────────


async def recall_for_message_via_gateway(
    gateway: Any,
    user_message: str,
    *,
    k: int = 4,
    min_similarity: float = 0.72,
    timeout_s: float = 3.0,
    exclude_buckets: Sequence[str] | None = None,
) -> list[RecallResult]:
    """Intelligent recall pipeline: gate → classify → targeted hybrid search.

    Phase 3: replaces the Phase-1 transparent passthrough with:
      1. Heuristic gate (skip trivial turns)
      2. Bucket classifier (restrict search to relevant domains)
      3. Targeted hybrid recall via Gateway.targeted_recall()

    Falls back to the legacy ``auto_recall`` path when the Gateway
    doesn't expose ``targeted_recall``.
    """
    # Layer 1 — Gate.
    if not should_recall_heuristic(user_message):
        _log.debug("gateway_recall.gate_dropped msg=%r", user_message[:40])
        if gateway is not None:
            _inc_metric(gateway, "recall_gate_skipped")
        return []

    svc = gateway.memory_service if gateway else None
    if svc is None:
        return []

    # Layer 2 — Classify.
    # Try semantic (embedding) first; fallback to keyword heuristic.
    _embedder = getattr(svc, "embedder", None) if svc else None
    relevant_buckets = await classify_buckets_semantic(
        user_message, _embedder,
    )
    if relevant_buckets:
        _log.debug(
            "gateway_recall.classified_semantic buckets=%s",
            ",".join(relevant_buckets),
        )
    else:
        relevant_buckets = classify_buckets_heuristic(user_message)
        if relevant_buckets:
            _log.debug(
                "gateway_recall.classified_heuristic buckets=%s",
                ",".join(relevant_buckets),
            )
    if relevant_buckets and gateway is not None:
        for b in relevant_buckets:
            _inc_metric(gateway, "recall_classify_buckets", b)

    # Layer 3 — Targeted search.
    if hasattr(gateway, "targeted_recall"):
        try:
            hits = await gateway.targeted_recall(
                query=user_message,
                buckets=relevant_buckets if relevant_buckets else None,
                k=k,
                min_similarity=min_similarity,
                timeout_s=timeout_s,
            )
            return hits
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "gateway_recall.targeted_failed err=%s — falling back to "
                "legacy auto_recall", exc,
            )
            # Fall through to legacy path.

    # Legacy fallback: direct auto_recall (Phase 1 path).
    try:
        from xmclaw.daemon.auto_recall import (
            RecalledFact,
            recall_for_message,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("gateway_recall.import_failed err=%s", exc)
        return []

    try:
        hits: list[RecalledFact] = await recall_for_message(
            memory_service=svc,
            user_message=user_message,
            k=k,
            min_similarity=min_similarity,
            exclude_buckets=exclude_buckets,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("gateway_recall.recall_failed err=%s", exc)
        return []

    return [
        RecallResult(
            fid=h.fid,
            text=h.text,
            bucket=h.bucket,
            kind=h.kind,
            similarity=h.similarity,
            ts_first=h.ts_first,
        )
        for h in hits
    ]


def render_recalled_block(hits: Sequence[RecallResult]) -> str:
    """Format recall hits as a ``<recalled>`` XML-ish block."""
    if not hits:
        return ""
    lines = ['<recalled relevance="similarity-top-k">']
    for h in hits:
        sim = f"{h.similarity:.2f}"
        bucket = h.bucket
        text = h.text.replace("\n", " ")
        suffix_fid = f" [fid:{h.fid}]" if h.fid else ""
        lines.append(f"- ({sim} | {bucket}) {text}{suffix_fid}")
    lines.append("</recalled>")
    return "\n".join(lines)


def prepend_recalled_block(user_message: str, block: str) -> str:
    """Convenience: prepend a rendered block to the user message."""
    if not block:
        return user_message
    return f"{block}\n\n{user_message}"


__all__ = [
    "should_recall_heuristic",
    "classify_buckets_heuristic",
    "recall_for_message_via_gateway",
    "render_recalled_block",
    "prepend_recalled_block",
]
