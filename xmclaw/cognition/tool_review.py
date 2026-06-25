"""Tool result review and failure strategy decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FailureDecision = Literal[
    "continue",
    "retry",
    "change_plan",
    "query_memory",
    "query_skill",
    "inspect_artifact",
    "ask_user",
    "stop",
]


@dataclass(frozen=True, slots=True)
class ToolReview:
    tool_name: str
    ok: bool
    decision: FailureDecision
    should_retry_same: bool
    reason: str
    recommended_actions: list[str] = field(default_factory=list)
    error_signature: str = ""
    repeated_count: int = 0

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "kind": "tool_review",
            "tool": self.tool_name,
            "ok": self.ok,
            "strategy_decision": self.decision,
            "should_retry_same": self.should_retry_same,
            "reason": self.reason,
            "recommended_action": "; ".join(self.recommended_actions),
            "recovery_options": list(self.recommended_actions),
            "error_signature": self.error_signature,
            "repeated_count": self.repeated_count,
        }


class ToolFailureStrategy:
    """Classify tool results into strategy-switch decisions."""

    def __init__(self, *, repeat_threshold: int = 2) -> None:
        self.repeat_threshold = max(1, int(repeat_threshold))

    def review(
        self,
        *,
        tool_name: str,
        ok: bool,
        error: str | None = None,
        content: str | None = None,
        recent_failures: list[dict[str, Any]] | None = None,
    ) -> ToolReview:
        if ok:
            return ToolReview(
                tool_name=tool_name,
                ok=True,
                decision="continue",
                should_retry_same=False,
                reason="tool succeeded",
            )

        signature = _error_signature(error or content or "")
        repeated = 1 + sum(
            1
            for item in recent_failures or []
            if str(item.get("tool") or item.get("tool_name") or "") == tool_name
            and _error_signature(str(item.get("error") or "")) == signature
        )

        lower = f"{error or ''}\n{content or ''}".lower()
        actions: list[str] = []
        decision: FailureDecision = "change_plan"
        should_retry_same = False

        if "path_not_found" in lower or "not found" in lower or "找不到" in lower:
            decision = "query_memory"
            actions.extend([
                "调用 memory_decision(action='search') 查询相关路径/历史失败经验",
                "检查 Artifact Ledger 中当前任务已产生的路径",
                "换搜索范围或先列父目录，不要重复同一路径搜索",
            ])
        elif "permission_denied" in lower or "permission denied" in lower:
            decision = "ask_user"
            actions.extend([
                "停止重复执行同一命令",
                "向用户确认权限或选择可写路径",
            ])
        elif "timeout" in lower or "timed out" in lower or "超时" in lower:
            decision = "change_plan"
            actions.extend([
                "缩小输入范围或拆分命令",
                "必要时提高 timeout，但不要无差别重试",
            ])
        else:
            actions.extend([
                "调用 skill_browse 查询相关技能",
                "调整计划后再尝试替代工具",
            ])

        if repeated >= self.repeat_threshold:
            should_retry_same = False
            if "调用 skill_browse 查询相关技能" not in actions:
                actions.append("调用 skill_browse 查询相关技能")
            actions.append("连续失败已达阈值，禁止原样重试")
        elif decision == "change_plan":
            should_retry_same = False

        return ToolReview(
            tool_name=tool_name,
            ok=False,
            decision=decision,
            should_retry_same=should_retry_same,
            reason="tool failed; strategy switch required",
            recommended_actions=actions,
            error_signature=signature,
            repeated_count=repeated,
        )


def _error_signature(text: str) -> str:
    clean = " ".join((text or "").strip().lower().split())
    if not clean:
        return "unknown_error"
    return clean[:160]


__all__ = ["FailureDecision", "ToolFailureStrategy", "ToolReview"]
