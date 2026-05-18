"""EvolutionLoop — 持续自我进化循环。

整合 evolution_agent.py 的逻辑，基于经验优化：
- 技能推荐（SkillPromoter）
- 系统提示进化（SystemPromptEvolver）
- 成本/延迟分析（PerformanceAnalyzer）
- 模式提取（PatternExtractor）

设计决策：evolution 不直接改写文件，而是写入 `proposals/` 目录，
由人类或更高权限代理审批后应用。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    """一个进化提案。"""

    id: str
    type: str  # "skill_promote" | "prompt_evolve" | "pattern_extract" | "perf_tuning"
    description: str
    target: str  # 影响的目标文件/模块
    diff: str  # 建议的 diff
    confidence: float = 0.0  # 0-1
    evidence: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | rejected | applied


class SkillPromoter:
    """分析工具使用频率，推荐创建技能。"""

    PROMOTION_THRESHOLD = 3  # 使用次数阈值

    def __init__(self, bus: Any | None = None) -> None:
        self._bus = bus
        self._tool_usage: dict[str, int] = {}

    def record_tool_call(self, tool_name: str) -> None:
        """记录工具调用。"""
        self._tool_usage[tool_name] = self._tool_usage.get(tool_name, 0) + 1

    async def analyze(self) -> list[EvolutionProposal]:
        """分析并生成技能推荐提案。"""
        proposals: list[EvolutionProposal] = []
        for tool_name, count in self._tool_usage.items():
            if count >= self.PROMOTION_THRESHOLD:
                proposals.append(
                    EvolutionProposal(
                        id=uuid.uuid4().hex,
                        type="skill_promote",
                        description=f"工具 '{tool_name}' 已使用 {count} 次，建议创建技能",
                        target=f"skills/{tool_name}.md",
                        diff=f"# 建议为 {tool_name} 创建 SKILL.md",
                        confidence=min(count / 10, 0.9),
                        evidence=[f"使用次数: {count}"],
                    )
                )
        return proposals


class SystemPromptEvolver:
    """分析失败模式，进化系统提示。"""

    def __init__(self, bus: Any | None = None) -> None:
        self._bus = bus
        self._failure_patterns: list[dict[str, Any]] = []

    def record_failure(self, context: str, error: str, recovery: str) -> None:
        """记录失败事件。"""
        self._failure_patterns.append(
            {
                "context": context,
                "error": error,
                "recovery": recovery,
                "timestamp": time.time(),
            }
        )

    async def analyze(self, current_prompt: str) -> list[EvolutionProposal]:
        """分析失败模式并生成提示改进提案。"""
        if not self._failure_patterns:
            return []

        # 简单启发式：如果同一错误出现 2 次以上，建议添加规则
        error_counts: dict[str, int] = {}
        for fp in self._failure_patterns:
            error_counts[fp["error"]] = error_counts.get(fp["error"], 0) + 1

        proposals: list[EvolutionProposal] = []
        for error, count in error_counts.items():
            if count >= 2:
                proposals.append(
                    EvolutionProposal(
                        id=uuid.uuid4().hex,
                        type="prompt_evolve",
                        description=f"检测到重复错误 '{error[:50]}...'，建议更新系统提示",
                        target="system_prompt",
                        diff=f"# 建议添加规则: 避免 {error[:100]}",
                        confidence=min(count / 5, 0.8),
                        evidence=[f"出现次数: {count}"],
                    )
                )
        return proposals


class PerformanceAnalyzer:
    """分析性能指标，生成优化提案。"""

    def __init__(self, bus: Any | None = None) -> None:
        self._bus = bus
        self._latencies: list[float] = []
        self._costs: list[float] = []

    def record_turn(self, latency_ms: float, cost_usd: float) -> None:
        """记录一轮对话的指标。"""
        self._latencies.append(latency_ms)
        self._costs.append(cost_usd)
        # 保持最近 1000 条
        if len(self._latencies) > 1000:
            self._latencies = self._latencies[-1000:]
            self._costs = self._costs[-1000:]

    async def analyze(self) -> list[EvolutionProposal]:
        """分析性能趋势。"""
        if not self._latencies:
            return []

        avg_latency = sum(self._latencies) / len(self._latencies)
        avg_cost = sum(self._costs) / len(self._costs) if self._costs else 0

        proposals: list[EvolutionProposal] = []
        if avg_latency > 5000:  # > 5s
            proposals.append(
                EvolutionProposal(
                    id=uuid.uuid4().hex,
                    type="perf_tuning",
                    description=f"平均延迟 {avg_latency:.0f}ms 过高，建议优化",
                    target="performance",
                    diff="# 建议: 减少 context 长度、使用更小的模型",
                    confidence=min(avg_latency / 10000, 0.9),
                    evidence=[f"avg_latency={avg_latency:.0f}ms, avg_cost=${avg_cost:.4f}"],
                )
            )
        return proposals


class PatternExtractor:
    """从成功交互中提取可复用模式。"""

    def __init__(self, bus: Any | None = None) -> None:
        self._bus = bus
        self._successful_patterns: list[dict[str, Any]] = []

    def record_success(self, task: str, approach: str, result: str) -> None:
        """记录成功模式。"""
        self._successful_patterns.append(
            {
                "task": task,
                "approach": approach,
                "result": result,
                "timestamp": time.time(),
            }
        )

    async def analyze(self) -> list[EvolutionProposal]:
        """提取重复成功模式。"""
        # 简化：返回最近 3 个成功模式的摘要
        if len(self._successful_patterns) < 3:
            return []

        proposals: list[EvolutionProposal] = []
        # 按 task 分组，找出重复任务
        task_groups: dict[str, list[dict[str, Any]]] = {}
        for p in self._successful_patterns:
            task_groups.setdefault(p["task"], []).append(p)

        for task, patterns in task_groups.items():
            if len(patterns) >= 3:
                proposals.append(
                    EvolutionProposal(
                        id=uuid.uuid4().hex,
                        type="pattern_extract",
                        description=f"任务 '{task[:50]}...' 成功模式已出现 {len(patterns)} 次",
                        target="patterns",
                        diff=f"# 建议创建 pattern: {task[:100]}",
                        confidence=min(len(patterns) / 10, 0.85),
                        evidence=[f"成功次数: {len(patterns)}"],
                    )
                )
        return proposals


class EvolutionLoop:
    """进化循环 — 定期运行进化代理，生成提案。"""

    def __init__(
        self,
        *,
        proposals_dir: Path | str | None = None,
        bus: Any | None = None,
        interval_seconds: float = 3600.0,  # 默认 1 小时
        agent_loop: Any | None = None,
    ) -> None:
        # Patch A (2026-05-10): paths.evolution_proposals_dir() so
        # XMC_DATA_DIR overrides reroute (pre-fix this hand-built
        # the path regardless of env).
        if proposals_dir is None:
            from xmclaw.utils.paths import evolution_proposals_dir
            proposals_dir = evolution_proposals_dir()
        self.proposals_dir = Path(proposals_dir)
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self._bus = bus
        self._interval = interval_seconds
        self._agent_loop = agent_loop
        self._running = False
        self._task: asyncio.Task[Any] | None = None

        # 子模块
        self.skill_promoter = SkillPromoter(bus=bus)
        self.prompt_evolver = SystemPromptEvolver(bus=bus)
        self.perf_analyzer = PerformanceAnalyzer(bus=bus)
        self.pattern_extractor = PatternExtractor(bus=bus)

    # ── 公共 API ──

    def record_tool_call(self, tool_name: str) -> None:
        """记录工具调用（供 AgentLoop 调用）。"""
        self.skill_promoter.record_tool_call(tool_name)

    def record_turn(self, latency_ms: float, cost_usd: float) -> None:
        """记录一轮性能指标。"""
        self.perf_analyzer.record_turn(latency_ms, cost_usd)

    def record_failure(self, context: str, error: str, recovery: str) -> None:
        """记录失败事件。"""
        self.prompt_evolver.record_failure(context, error, recovery)

    def record_success(self, task: str, approach: str, result: str) -> None:
        """记录成功模式。"""
        self.pattern_extractor.record_success(task, approach, result)

    # ── 生命周期 ──

    async def start(self) -> None:
        """启动进化循环。"""
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="evolution-loop")

    async def stop(self) -> None:
        """停止进化循环。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def trigger_once(self) -> list[EvolutionProposal]:
        """立即执行一次进化分析。"""
        proposals: list[EvolutionProposal] = []
        proposals.extend(await self.skill_promoter.analyze())
        proposals.extend(await self.prompt_evolver.analyze(""))
        proposals.extend(await self.perf_analyzer.analyze())
        proposals.extend(await self.pattern_extractor.analyze())

        # Wave-32+ auto-approval. Resolve flag values once per batch
        # so we don't repeat the lookup per proposal. Defaults
        # mirror the registry values; explicit fallbacks here keep
        # the loop usable even when the feature_flags engine isn't
        # wired (test contexts, embedded calls).
        enabled, threshold = _resolve_auto_approve_config()
        approved_count = 0
        for p in proposals:
            if enabled and p.confidence >= threshold:
                # Replace with an approved variant. EvolutionProposal
                # is frozen so we copy + override status. Persistence
                # writes the new status directly — no second
                # round-trip.
                approved = _with_status(p, "approved")
                await self._write_proposal(approved)
                approved_count += 1
            else:
                await self._write_proposal(p)

        if approved_count:
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).info(
                    "evolution.auto_approved count=%d threshold=%.2f total=%d",
                    approved_count, threshold, len(proposals),
                )
            except Exception:  # noqa: BLE001 — never block trigger on telemetry
                pass

        return proposals

    async def _run_loop(self) -> None:
        """主循环。"""
        while self._running:
            try:
                await asyncio.wait_for(
                    asyncio.sleep(self._interval),
                    timeout=self._interval + 10,
                )
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            await self.trigger_once()

    # ── 内部 ──

    async def _write_proposal(self, proposal: EvolutionProposal) -> None:
        """将提案写入文件。"""
        path = self.proposals_dir / f"{proposal.id}.json"
        data = {
            "id": proposal.id,
            "type": proposal.type,
            "description": proposal.description,
            "target": proposal.target,
            "diff": proposal.diff,
            "confidence": proposal.confidence,
            "evidence": proposal.evidence,
            "created_at": proposal.created_at,
            "status": proposal.status,
        }
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        )

    async def list_pending(self) -> list[EvolutionProposal]:
        """列出待处理的提案。"""
        proposals: list[EvolutionProposal] = []
        for path in self.proposals_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") == "pending":
                    proposals.append(
                        EvolutionProposal(
                            id=data["id"],
                            type=data["type"],
                            description=data["description"],
                            target=data["target"],
                            diff=data["diff"],
                            confidence=data.get("confidence", 0.0),
                            evidence=data.get("evidence", []),
                            created_at=data.get("created_at", 0.0),
                            status=data["status"],
                        )
                    )
            except (json.JSONDecodeError, KeyError):
                continue
        return proposals

    async def auto_approve_pending(self) -> dict[str, int]:
        """Backfill pass — scan every ``status="pending"`` proposal on
        disk and auto-approve any whose confidence clears the current
        threshold. Returns ``{approved, kept_pending, skipped_errors}``.

        Use case: after enabling auto-approve, the existing pending
        pile was written before the feature was live. Calling this
        once clears the backlog without waiting for the next
        ``trigger_once`` cycle.

        Idempotent — already-approved proposals are skipped because
        the pending-only filter excludes them.
        """
        enabled, threshold = _resolve_auto_approve_config()
        if not enabled:
            return {"approved": 0, "kept_pending": 0, "skipped_errors": 0}
        approved = 0
        kept = 0
        errors = 0
        for path in self.proposals_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                errors += 1
                continue
            if data.get("status") != "pending":
                continue
            conf = float(data.get("confidence") or 0.0)
            if conf >= threshold:
                data["status"] = "approved"
                try:
                    path.write_text(
                        json.dumps(data, indent=2), encoding="utf-8",
                    )
                    approved += 1
                except OSError:
                    errors += 1
            else:
                kept += 1
        return {
            "approved": approved,
            "kept_pending": kept,
            "skipped_errors": errors,
        }

    async def approve(self, proposal_id: str) -> bool:
        """批准一个提案（等待人类或外部代理应用）。"""
        path = self.proposals_dir / f"{proposal_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = "approved"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    async def reject(self, proposal_id: str) -> bool:
        """拒绝一个提案。"""
        path = self.proposals_dir / f"{proposal_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = "rejected"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False


# ── Wave-32+ auto-approval helpers ──────────────────────────────────────


def _with_status(p: EvolutionProposal, status: str) -> EvolutionProposal:
    """Return a copy of ``p`` with a new status. EvolutionProposal is
    frozen so we have to rebuild — kept as a tiny helper to avoid
    inline dataclasses.replace noise at the call site."""
    return EvolutionProposal(
        id=p.id, type=p.type, description=p.description,
        target=p.target, diff=p.diff, confidence=p.confidence,
        evidence=p.evidence, created_at=p.created_at, status=status,
    )


def _resolve_auto_approve_config() -> tuple[bool, float]:
    """Read the auto-approve flags from the FeatureFlagEngine.

    Returns ``(enabled, threshold)``. On any error (engine not
    wired, flags not registered, etc) falls back to (True, 0.8) —
    same defaults as the registry — so the loop keeps working in
    test contexts and pre-flag-engine deployments.
    """
    try:
        from xmclaw.core.feature_flags import default_engine
        eng = default_engine()
        enabled = bool(eng.variant(
            "evolution.auto_approve.enabled", default=True,
        ))
        threshold_raw = eng.variant(
            "evolution.auto_approve.threshold", default=0.8,
        )
        try:
            threshold = float(threshold_raw)
        except (TypeError, ValueError):
            threshold = 0.8
        # Clamp into a sane range — a misconfigured 1.5 should
        # disable rather than reject everything; a -1 should approve
        # nothing.
        threshold = max(0.0, min(1.0, threshold))
        return enabled, threshold
    except Exception:  # noqa: BLE001
        return True, 0.8
