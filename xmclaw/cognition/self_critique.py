"""Reflexion-style self-critique primitives for failed trajectories.

The periodic ReflectionCycle is useful for slow consolidation. Failed turns
need a different path: immediate, structured critique with a bounded memory
materialization policy. This module is dependency-free and does not call an
LLM or write memory directly.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Literal


CritiqueTrigger = Literal[
    "failed_turn",
    "max_hops_exit",
    "stuck_loop_exit",
    "low_grader_score",
    "plan_failed",
    "tool_error",
]

CritiqueDimension = Literal[
    "plan_quality",
    "tool_choice",
    "evidence",
    "safety",
    "user_fit",
    "retry_decision",
]

CRITIQUE_DIMENSIONS: tuple[CritiqueDimension, ...] = (
    "plan_quality",
    "tool_choice",
    "evidence",
    "safety",
    "user_fit",
    "retry_decision",
)

SELF_CRITIQUE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "trigger",
        "diagnosis",
        "dimension_scores",
        "lesson",
        "retry_decision",
        "confidence",
    ],
    "properties": {
        "trigger": {"type": "string"},
        "diagnosis": {"type": "string"},
        "dimension_scores": {
            "type": "object",
            "properties": {
                name: {"type": "number", "minimum": 0, "maximum": 1}
                for name in CRITIQUE_DIMENSIONS
            },
        },
        "lesson": {"type": "string"},
        "retry_decision": {
            "type": "string",
            "enum": ["retry", "ask_user", "change_plan", "stop"],
        },
        "memory_worthy": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


@dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    kind: str
    content: str
    ok: bool | None = None
    tool_name: str = ""
    error: str = ""

    def to_prompt_line(self, index: int) -> str:
        status = "unknown" if self.ok is None else ("ok" if self.ok else "failed")
        parts = [f"{index}. kind={self.kind}", f"status={status}"]
        if self.tool_name:
            parts.append(f"tool={self.tool_name}")
        if self.error:
            parts.append(f"error={_compact(self.error, 240)}")
        parts.append(f"content={_compact(self.content, 600)}")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "ok": self.ok,
            "tool_name": self.tool_name,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SelfCritiqueRequest:
    trigger: CritiqueTrigger
    session_id: str = ""
    goal: str = ""
    failure_summary: str = ""
    trajectory: tuple[TrajectoryEvent, ...] = ()
    graph_state: dict[str, Any] = field(default_factory=dict)
    grader_score: float | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class SelfCritique:
    trigger: str
    diagnosis: str
    dimension_scores: dict[str, float]
    lesson: str
    retry_decision: str
    memory_worthy: bool
    confidence: float
    raw: dict[str, Any] = field(default_factory=dict)

    def to_memory_text(self) -> str:
        return (
            f"When trigger={self.trigger}, lesson={self.lesson} "
            f"Retry decision: {self.retry_decision}. Diagnosis: {self.diagnosis}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "diagnosis": self.diagnosis,
            "dimension_scores": dict(self.dimension_scores),
            "lesson": self.lesson,
            "retry_decision": self.retry_decision,
            "memory_worthy": self.memory_worthy,
            "confidence": self.confidence,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True, slots=True)
class SelfCritiqueMemoryCandidate:
    text: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "metadata": dict(self.metadata)}


@dataclass(frozen=True, slots=True)
class SelfCritiqueMaterializationResult:
    """Outcome of trying to persist a critique lesson."""

    status: Literal["written", "skipped", "failed"]
    reason: str = ""
    candidate: SelfCritiqueMemoryCandidate | None = None
    fact_id: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "candidate": (
                self.candidate.to_dict() if self.candidate is not None else None
            ),
            "fact_id": self.fact_id,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SelfCritiqueRunResult:
    """End-to-end Reflexion pass result."""

    status: Literal["completed", "skipped", "failed"]
    request: SelfCritiqueRequest
    critique: SelfCritique | None = None
    materialization: SelfCritiqueMaterializationResult | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "request": {
                "trigger": self.request.trigger,
                "session_id": self.request.session_id,
                "goal": self.request.goal,
                "failure_summary": self.request.failure_summary,
                "trajectory_events": len(self.request.trajectory),
            },
            "critique": self.critique.to_dict() if self.critique else None,
            "materialization": (
                self.materialization.to_dict()
                if self.materialization is not None
                else None
            ),
            "error": self.error,
        }


class SelfCritiquePromptBuilder:
    def build(self, request: SelfCritiqueRequest) -> str:
        trajectory = "\n".join(
            ev.to_prompt_line(i + 1) for i, ev in enumerate(request.trajectory)
        ) or "(no trajectory events provided)"
        graph_state = _json_block(request.graph_state) if request.graph_state else "{}"
        grader = (
            "unknown" if request.grader_score is None
            else f"{float(request.grader_score):.3f}"
        )
        schema = _json_block(SELF_CRITIQUE_JSON_SCHEMA)
        return f"""You are XMclaw's Reflexion critic for failed agent trajectories.

Critique the failure without blaming the user and without inventing facts.
Score every dimension from 0.0 to 1.0 where 1.0 is strong.

Dimensions:
- plan_quality: Was the plan decomposed and sequenced well?
- tool_choice: Were the selected tools appropriate and economical?
- evidence: Did the agent verify claims with enough evidence?
- safety: Did the agent avoid risky, destructive, or policy-unsafe behavior?
- user_fit: Did the behavior match the user's latest intent and constraints?
- retry_decision: Is the next retry/stop/ask decision justified?

Context:
- session_id: {request.session_id or "unknown"}
- trigger: {request.trigger}
- goal: {_compact(request.goal, 800)}
- failure_summary: {_compact(request.failure_summary, 1200)}
- grader_score: {grader}

Trajectory:
{trajectory}

GraphState snapshot:
{graph_state}

Return strict JSON only, matching this schema:
{schema}
"""


def parse_self_critique_json(text: str) -> SelfCritique:
    payload = _loads_json_object(text)
    scores = payload.get("dimension_scores") or {}
    normalized_scores = {
        name: _clamp_float(scores.get(name, 0.0))
        for name in CRITIQUE_DIMENSIONS
    }
    retry = str(payload.get("retry_decision") or "stop").strip()
    if retry not in {"retry", "ask_user", "change_plan", "stop"}:
        retry = "stop"
    return SelfCritique(
        trigger=str(payload.get("trigger") or "failed_turn"),
        diagnosis=str(payload.get("diagnosis") or "").strip(),
        dimension_scores=normalized_scores,
        lesson=str(payload.get("lesson") or "").strip(),
        retry_decision=retry,
        memory_worthy=bool(payload.get("memory_worthy", False)),
        confidence=_clamp_float(payload.get("confidence", 0.0)),
        raw=payload,
    )


class SelfCritiqueMemoryPolicy:
    """Bound critiques before a caller writes them into long-term memory."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.55,
        min_lesson_chars: int = 20,
        cooldown_seconds: float = 3600.0,
        max_per_session: int = 3,
    ) -> None:
        self.min_confidence = _clamp_float(min_confidence)
        self.min_lesson_chars = max(1, int(min_lesson_chars))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.max_per_session = max(1, int(max_per_session))
        self._last_by_key: dict[str, float] = {}
        self._count_by_session: dict[str, int] = {}

    def candidate(
        self,
        critique: SelfCritique,
        *,
        session_id: str = "",
        now: float | None = None,
    ) -> SelfCritiqueMemoryCandidate | None:
        if not critique.memory_worthy:
            return None
        if critique.confidence < self.min_confidence:
            return None
        if len(critique.lesson.strip()) < self.min_lesson_chars:
            return None
        key = self._key(critique)
        ts = time.time() if now is None else float(now)
        last = self._last_by_key.get(key)
        if last is not None and ts - last < self.cooldown_seconds:
            return None
        session_key = session_id or "_global"
        if self._count_by_session.get(session_key, 0) >= self.max_per_session:
            return None
        self._last_by_key[key] = ts
        self._count_by_session[session_key] = (
            self._count_by_session.get(session_key, 0) + 1
        )
        return SelfCritiqueMemoryCandidate(
            text=critique.to_memory_text(),
            metadata={
                "source": "self_critique",
                "session_id": session_id,
                "trigger": critique.trigger,
                "retry_decision": critique.retry_decision,
                "confidence": critique.confidence,
                "session_write_count": self._count_by_session[session_key],
                "session_write_limit": self.max_per_session,
            },
        )

    @staticmethod
    def _key(critique: SelfCritique) -> str:
        lesson = " ".join(critique.lesson.lower().split())
        return f"{critique.trigger}:{lesson[:160]}"


class SelfCritiqueMaterializer:
    """Persist policy-approved self-critiques into long-term memory.

    The materializer accepts a memory service object instead of importing the
    memory package. That keeps cognition as the caller-side integration layer
    and avoids a reverse dependency from memory back into cognition.
    """

    def __init__(
        self,
        *,
        policy: SelfCritiqueMemoryPolicy | None = None,
        kind: str = "lesson",
        scope: str = "project",
        layer: str = "long_term",
        bucket: str = "failure_modes",
    ) -> None:
        self.policy = policy or SelfCritiqueMemoryPolicy()
        self.kind = kind
        self.scope = scope
        self.layer = layer
        self.bucket = bucket

    async def materialize(
        self,
        critique: SelfCritique,
        *,
        memory_service: Any | None,
        session_id: str = "",
        now: float | None = None,
    ) -> SelfCritiqueMaterializationResult:
        if memory_service is None:
            return SelfCritiqueMaterializationResult(
                status="skipped",
                reason="memory_service_missing",
            )
        candidate = self.policy.candidate(
            critique,
            session_id=session_id,
            now=now,
        )
        if candidate is None:
            return SelfCritiqueMaterializationResult(
                status="skipped",
                reason="policy_rejected",
            )
        remember = getattr(memory_service, "remember", None)
        if remember is None:
            return SelfCritiqueMaterializationResult(
                status="skipped",
                reason="memory_service_missing_remember",
                candidate=candidate,
            )
        try:
            fact = await remember(
                candidate.text,
                kind=self.kind,
                scope=self.scope,
                layer=self.layer,
                confidence=float(candidate.metadata.get("confidence", 0.7)),
                bucket=self.bucket,
                provenance="self_critique",
            )
        except Exception as exc:  # noqa: BLE001
            return SelfCritiqueMaterializationResult(
                status="failed",
                reason="memory_write_failed",
                candidate=candidate,
                error=f"{type(exc).__name__}: {exc}",
            )
        return SelfCritiqueMaterializationResult(
            status="written",
            reason="memory_write_ok",
            candidate=candidate,
            fact_id=str(getattr(fact, "id", "") or ""),
        )


class SelfCritiqueEngine:
    """Run the Reflexion request -> JSON critique -> memory path.

    ``critic_call`` is injected so daemon/factory code can choose an LLM,
    a test double, or a disabled no-op without this module importing any
    provider.
    """

    def __init__(
        self,
        *,
        prompt_builder: SelfCritiquePromptBuilder | None = None,
        materializer: SelfCritiqueMaterializer | None = None,
    ) -> None:
        self.prompt_builder = prompt_builder or SelfCritiquePromptBuilder()
        self.materializer = materializer or SelfCritiqueMaterializer()

    async def run(
        self,
        request: SelfCritiqueRequest,
        *,
        critic_call: Callable[[str], Awaitable[str]] | None,
        memory_service: Any | None = None,
        materialize: bool = True,
        now: float | None = None,
    ) -> SelfCritiqueRunResult:
        if critic_call is None:
            return SelfCritiqueRunResult(
                status="skipped",
                request=request,
                error="critic_call_missing",
            )
        try:
            prompt = self.prompt_builder.build(request)
            raw = await critic_call(prompt)
            critique = parse_self_critique_json(raw)
        except Exception as exc:  # noqa: BLE001
            return SelfCritiqueRunResult(
                status="failed",
                request=request,
                error=f"{type(exc).__name__}: {exc}",
            )
        materialization = None
        if materialize:
            materialization = await self.materializer.materialize(
                critique,
                memory_service=memory_service,
                session_id=request.session_id,
                now=now,
            )
        return SelfCritiqueRunResult(
            status="completed",
            request=request,
            critique=critique,
            materialization=materialization,
        )


def _loads_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("self-critique response must be a JSON object")
    return data


def _clamp_float(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _compact(text: Any, limit: int) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    head = max(1, limit - 40)
    return s[:head] + f"... [truncated {len(s) - head} chars]"


def _json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


__all__ = [
    "CRITIQUE_DIMENSIONS",
    "SELF_CRITIQUE_JSON_SCHEMA",
    "CritiqueDimension",
    "CritiqueTrigger",
    "SelfCritique",
    "SelfCritiqueMemoryCandidate",
    "SelfCritiqueMaterializationResult",
    "SelfCritiqueMaterializer",
    "SelfCritiqueMemoryPolicy",
    "SelfCritiquePromptBuilder",
    "SelfCritiqueRequest",
    "SelfCritiqueRunResult",
    "SelfCritiqueEngine",
    "TrajectoryEvent",
    "parse_self_critique_json",
]
