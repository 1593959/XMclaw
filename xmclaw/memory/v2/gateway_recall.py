"""Gateway recall planning and targeted retrieval."""

from __future__ import annotations

import math
from typing import Any, Sequence

from xmclaw.memory.v2.gateway_models import RecallPlan, RecallResult
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

_MIN_RECALL_CHARS = 6

_GREETINGS = frozenset({
    "你好", "您好", "hi", "hello", "hey", "在吗", "在么",
    "早上好", "晚上好", "下午好", "哈喽", "hola",
})

_CONFIRMATIONS = frozenset({
    "好的", "好", "ok", "okay", "没问题", "可以", "行", "嗯", "哦",
    "知道了", "明白了", "谢谢", "多谢", "辛苦了", "是的", "对的",
    "没错", "嗯嗯", "oo", "o",
})

_RECALL_SIGNALS = frozenset({
    "记住", "记下", "别忘", "记着", "以后", "默认", "一直", "永远",
    "不要", "不能", "必须", "禁止", "上次", "之前", "继续", "类似",
    "同样", "偏好", "喜欢", "规则", "规律", "经验", "教训",
})

_BUCKET_KEYWORDS: dict[str, frozenset[str]] = {
    "project_fact": frozenset({
        "项目", "仓库", "代码", "部署", "配置", "版本", "需求", "服务器",
        "地址", "数据库", "接口", "端口", "路径", "桌面", "磁盘", "目录",
        "安装", "下载", "文件", "工作区", "依赖", "环境变量",
    }),
    "workflow": frozenset({
        "流程", "步骤", "规划", "计划", "执行", "任务", "检查", "怎么",
        "如何", "怎样", "做法", "方法", "顺序", "先", "再",
    }),
    "tool_quirks": frozenset({
        "工具", "命令", "脚本", "参数", "选项", "bug", "错误", "失败",
        "报错", "shell", "powershell", "python", "npm", "浏览器",
    }),
    "failure_modes": frozenset({
        "失败", "报错", "异常", "卡住", "超时", "无法", "不能", "不行",
        "没反应", "没结果", "找不到", "没找到", "死磕", "重复",
    }),
    "user_preference": frozenset({
        "喜欢", "偏好", "习惯", "想要", "希望", "需要", "不要", "别",
        "永远", "总是", "从不", "禁止", "中文", "英文", "简洁", "详细",
    }),
    "user_identity": frozenset({
        "我是", "我叫", "我的名字", "称呼", "叫我", "公司", "团队",
        "行业", "业务", "职业", "工作", "职位", "角色", "全名",
    }),
    "rules": frozenset({
        "规则", "约束", "限制", "必须", "务必", "一定", "禁止", "不能",
        "不准", "不可", "别", "不要", "永远别", "绝对",
    }),
    "values": frozenset({
        "价值观", "原则", "理念", "文化", "信条", "信念", "追求", "使命",
        "愿景",
    }),
    "procedural": frozenset({
        "规律", "经验", "教训", "下次", "以后遇到", "类似任务", "流程固化",
        "抽象", "总结", "复用", "技能", "策略切换",
    }),
}

_BUCKET_DESCRIPTIONS: dict[str, str] = {
    "project_fact": "项目、仓库、代码、配置、部署、版本、路径、依赖等具体事实",
    "workflow": "操作流程、步骤顺序、最佳实践、工作方式和规范",
    "tool_quirks": "工具、命令、参数、脚本、浏览器、报错和工具行为",
    "failure_modes": "失败、异常、卡住、超时、错误排查和故障处理",
    "user_preference": "用户偏好、习惯、语言风格、默认选择和禁忌",
    "user_identity": "用户身份、名字、称呼、公司、团队、行业、职业和业务领域",
    "rules": "长期规则、硬约束、必须做什么、禁止做什么",
    "values": "价值观、原则、理念、文化、信念和长期追求",
    "procedural": "可复用经验、规律、教训、抽象流程和技能候选",
}

_bucket_desc_vec_cache: dict[str, dict[str, tuple[float, ...]]] = {}


def should_recall_heuristic(user_message: str) -> bool:
    """Return whether the current turn should query long-term memory."""
    text = (user_message or "").strip()
    if not text:
        return False
    normalized = text.lower().strip("。！？?!~\"'“”‘’ ")
    if normalized in _GREETINGS or normalized in _CONFIRMATIONS:
        return False
    has_signal = any(signal in normalized for signal in _RECALL_SIGNALS)
    if len(normalized) < _MIN_RECALL_CHARS and not has_signal:
        return False
    return any("\u4e00" <= ch <= "\u9fff" or ch.isalpha() or ch.isdigit() for ch in normalized)


def classify_buckets_heuristic(user_message: str) -> list[str]:
    """Keyword-driven bucket classifier used when semantic classify is absent."""
    text = (user_message or "").lower()
    matched: list[str] = []
    for bucket, keywords in _BUCKET_KEYWORDS.items():
        if any(keyword.lower() in text for keyword in keywords):
            matched.append(bucket)
    return matched


def build_recall_plan(user_message: str) -> RecallPlan:
    """Build a deterministic recall plan before executing retrieval."""
    if not should_recall_heuristic(user_message):
        return RecallPlan(need_recall=False)

    buckets = classify_buckets_heuristic(user_message)
    query = " ".join((user_message or "").split())
    expansions: list[str] = [query]
    if "procedural" in buckets or "workflow" in buckets:
        expansions.append("可复用流程 经验 教训 下次怎么做")
    if "failure_modes" in buckets or "tool_quirks" in buckets:
        expansions.append("历史失败 替代方法 工具限制 已验证解决办法")
    if "user_preference" in buckets or "rules" in buckets:
        expansions.append("用户偏好 长期规则 默认约束 禁忌")

    return RecallPlan(
        need_recall=True,
        relevant_buckets=buckets,
        query_expansion=" ".join(part for part in expansions if part).strip(),
    )


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
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
    """Embedding-based semantic bucket classifier."""
    if embedder is None:
        return []
    text = (user_message or "").strip()
    if len(text) < 4:
        return []
    try:
        qvec = tuple(await embedder.embed(text))
        cache_key = getattr(embedder, "model_name", "default")
        cached = _bucket_desc_vec_cache.get(cache_key)
        if cached is None:
            descs = list(_BUCKET_DESCRIPTIONS.items())
            desc_vectors = await embedder.embed_batch([desc for _, desc in descs])
            cached = {
                bucket: tuple(dvec)
                for (bucket, _), dvec in zip(descs, desc_vectors)
            }
            _bucket_desc_vec_cache[cache_key] = cached
        scores = [
            (bucket, _cosine_similarity(qvec, dvec))
            for bucket, dvec in cached.items()
        ]
        scores.sort(key=lambda item: item[1], reverse=True)
        return [bucket for bucket, score in scores[:top_k] if score >= threshold]
    except Exception as exc:  # noqa: BLE001
        _log.debug("classify.semantic_failed err=%s", exc)
        return []


async def recall_for_message_via_gateway(
    gateway: Any,
    user_message: str,
    *,
    k: int = 4,
    min_similarity: float = 0.72,
    timeout_s: float = 3.0,
    exclude_buckets: Sequence[str] | None = None,
) -> list[RecallResult]:
    """Run recall gate, classify buckets, then execute targeted recall."""
    text = (user_message or "").strip()
    gate_enabled = bool(getattr(gateway, "_recall_gate", True))
    classify_enabled = bool(getattr(gateway, "_recall_classify", True))

    plan = build_recall_plan(text)
    if gate_enabled and not plan.need_recall:
        _log.debug("gateway_recall.gate_dropped msg=%r", text[:40])
        _inc_metric(gateway, "recall_gate_skipped")
        return []
    if not plan.need_recall:
        plan = RecallPlan(need_recall=True, query_expansion=text)

    svc = gateway.memory_service if gateway else None
    if svc is None:
        return []

    relevant_buckets: list[str] = []
    if classify_enabled:
        embedder = getattr(svc, "embedder", None)
        relevant_buckets = await classify_buckets_semantic(text, embedder)
        if not relevant_buckets:
            relevant_buckets = list(plan.relevant_buckets)
    else:
        relevant_buckets = list(plan.relevant_buckets)

    for bucket in relevant_buckets:
        _inc_metric(gateway, "recall_classify_buckets", bucket)

    recall_query = plan.query_expansion or text
    if hasattr(gateway, "targeted_recall"):
        try:
            hits = await gateway.targeted_recall(
                query=recall_query,
                buckets=relevant_buckets if relevant_buckets else None,
                k=k,
                min_similarity=min_similarity,
                timeout_s=timeout_s,
            )
            return _merge_recall_hits([], [
                _explain_recall_hit(hit, query=recall_query)
                for hit in hits
            ], k=k)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "gateway_recall.targeted_failed err=%s; falling back to legacy auto_recall",
                exc,
            )

    try:
        from xmclaw.daemon.auto_recall import recall_for_message
    except Exception as exc:  # noqa: BLE001
        _log.warning("gateway_recall.import_failed err=%s", exc)
        return []

    try:
        legacy_hits = await recall_for_message(
            memory_service=svc,
            user_message=text,
            k=k,
            min_similarity=min_similarity,
            exclude_buckets=exclude_buckets,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("gateway_recall.recall_failed err=%s", exc)
        return []

    converted = [
        RecallResult(
            fid=hit.fid,
            text=hit.text,
            bucket=hit.bucket,
            kind=hit.kind,
            similarity=hit.similarity,
            ts_first=hit.ts_first,
            why_recalled=_why_recalled(hit.bucket, hit.kind, text),
            source="legacy_auto_recall",
            confidence=float(getattr(hit, "confidence", 0.0) or 0.0),
            validity="active",
            recommended_action=_recommended_action(hit.bucket, hit.kind),
        )
        for hit in legacy_hits
    ]
    return _merge_recall_hits([], converted, k=k)


def render_recalled_block(hits: Sequence[RecallResult]) -> str:
    """Format recall hits as a compact action-oriented block for the LLM."""
    if not hits:
        return ""
    lines = [
        "<memory-recall>",
        "使用方式：这些是本轮行动依据，不是用户新输入；若与用户本轮明确指令冲突，以用户本轮指令为准。",
    ]
    for hit in hits:
        text = hit.text.replace("\n", " ").strip()
        why = hit.why_recalled or _why_recalled(hit.bucket, hit.kind, "")
        action = hit.recommended_action or _recommended_action(hit.bucket, hit.kind)
        conf = f"{float(hit.confidence or hit.similarity or 0.0):.2f}"
        lines.append(
            "- "
            f"id={hit.fid or 'unknown'}; bucket={hit.bucket or 'misc'}; "
            f"kind={hit.kind or 'fact'}; confidence={conf}; "
            f"why={why}; action={action}; text={text}"
        )
    lines.append("</memory-recall>")
    return "\n".join(lines)


def _merge_recall_hits(
    priority_hits: Sequence[RecallResult],
    other_hits: Sequence[RecallResult],
    *,
    k: int,
) -> list[RecallResult]:
    merged: list[RecallResult] = []
    seen: set[str] = set()
    for hit in list(priority_hits) + list(other_hits):
        key = hit.text.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(hit)
        if len(merged) >= k:
            break
    return merged


def _explain_recall_hit(hit: RecallResult, *, query: str) -> RecallResult:
    if hit.why_recalled and hit.recommended_action:
        return hit
    return RecallResult(
        fid=hit.fid,
        text=hit.text,
        bucket=hit.bucket,
        kind=hit.kind,
        similarity=hit.similarity,
        ts_first=hit.ts_first,
        why_recalled=hit.why_recalled or _why_recalled(hit.bucket, hit.kind, query),
        source=hit.source or "gateway_targeted_recall",
        confidence=hit.confidence or max(0.0, min(1.0, float(hit.similarity or 0.0))),
        validity=hit.validity or "active",
        recommended_action=hit.recommended_action or _recommended_action(hit.bucket, hit.kind),
    )


def _why_recalled(bucket: str, kind: str, query: str) -> str:
    label = bucket or kind or "memory"
    suffix = f"；查询={query[:80]}" if query else ""
    return f"当前任务与 {label} 相关{suffix}"


def _recommended_action(bucket: str, kind: str) -> str:
    key = bucket or kind
    if key in {"rules", "user_preference", "user_identity"}:
        return "作为硬约束、身份事实或默认偏好执行"
    if key in {"failure_modes", "tool_quirks"}:
        return "避免重复历史失败；优先换工具、换路径或先验证假设"
    if key in {"workflow", "procedural"}:
        return "复用该流程；新经验必须在结果验证后再沉淀"
    if key == "project_fact":
        return "作为项目事实依据；必要时先验证是否仍然有效"
    return "作为辅助上下文参考，不覆盖用户本轮明确指令"


def prepend_recalled_block(user_message: str, block: str) -> str:
    """Prepend a rendered recall block to the user message."""
    if not block:
        return user_message
    return f"{block}\n\n{user_message}"


def _inc_metric(gateway: Any, key: str, sub_key: str | None = None) -> None:
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


__all__ = [
    "should_recall_heuristic",
    "classify_buckets_heuristic",
    "build_recall_plan",
    "classify_buckets_semantic",
    "recall_for_message_via_gateway",
    "render_recalled_block",
    "prepend_recalled_block",
]
