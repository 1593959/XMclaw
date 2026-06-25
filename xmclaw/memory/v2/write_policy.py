"""Write-time guardrails for long-term memory.

The memory system should learn from verified outcomes, not from every
intermediate thought, failed probe, or unfinished tool trajectory. This
module is deterministic on purpose: it protects the Gateway write path
before facts reach vector stores or persona renderers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from xmclaw.memory.v2.gateway_models import CognitiveDigest, Observation


@dataclass(slots=True, frozen=True)
class MemoryWritePolicyDecision:
    allow: bool
    reason: str


_UNVERIFIED_LESSON_RE = re.compile(
    r"(?:下次|以后|应该|可以尝试|尝试|可能|也许|计划|方法|方案|经验|规律|"
    r"推测|猜测|大概|似乎|next time|should|try|maybe|plan|approach|"
    r"possibly|probably|seems)",
    re.IGNORECASE,
)

_TRANSIENT_STATUS_RE = re.compile(
    r"(?:正在|准备|打算|尝试中|还没完成|未完成|失败了|报错|找不到|"
    r"running|trying|attempting|failed|error|not found|in progress)",
    re.IGNORECASE,
)


def assess_memory_write(
    observation: Observation,
    digest: CognitiveDigest,
) -> MemoryWritePolicyDecision:
    """Return whether a digest may be committed to durable memory."""

    md: dict[str, Any] = observation.metadata or {}
    source = (observation.source or "").strip()
    text = (digest.synthesized_text or observation.content or "").strip()

    if md.get("force_remember") is True or source in {"manual", "manual_ui"}:
        return MemoryWritePolicyDecision(True, "manual_or_forced")

    if md.get("task_completed") is False or str(md.get("task_status") or "").lower() in {
        "running",
        "pending",
        "in_progress",
        "retrying",
    }:
        return MemoryWritePolicyDecision(False, "task_not_completed")

    if not _has_terminal_evidence(md) and _TRANSIENT_STATUS_RE.search(text):
        return MemoryWritePolicyDecision(False, "transient_or_failed_status")

    if source == "tool_result":
        if md.get("tool_success") is False:
            return MemoryWritePolicyDecision(False, "unverified_tool_failure")
        if not _has_terminal_evidence(md):
            return MemoryWritePolicyDecision(False, "tool_result_not_terminal")

    if source in {"post_sampling", "cognition"} and not _has_terminal_evidence(md):
        if digest.kind == "lesson" or digest.bucket in {
            "workflow",
            "tool_quirks",
            "failure_modes",
            "procedural",
        }:
            return MemoryWritePolicyDecision(False, "unverified_extracted_lesson")

    if (
        digest.kind in {"lesson", "procedure"}
        and digest.confidence < 0.85
        and _UNVERIFIED_LESSON_RE.search(text)
        and not _has_terminal_evidence(md)
    ):
        return MemoryWritePolicyDecision(False, "speculative_lesson")

    return MemoryWritePolicyDecision(True, "allowed")


def _has_terminal_evidence(md: dict[str, Any]) -> bool:
    return any(
        md.get(key) is True
        for key in (
            "verified",
            "verified_outcome",
            "task_completed",
            "user_confirmed",
            "session_completed",
        )
    )


__all__ = ["MemoryWritePolicyDecision", "assess_memory_write"]
