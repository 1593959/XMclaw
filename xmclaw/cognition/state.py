"""CognitiveState — 系统的持续认知状态。

从 AgentLoop 的隐式状态（分散在 _histories, _frozen_prompts,
_curriculum_hint_fired 等私有属性中）提取为显式的、可持久化的
认知状态模型。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Goal:
    """一个目标。

    R2 (2026-05-10) 升级：从单字段描述扩展到「可被 HTN planner
    分解 + 调度 + 评估完成度」的结构化对象。原有字段保留语义，
    新增字段全部带默认值 → 既有调用点零迁移成本。

    新增字段：
    * ``success_criteria`` — 一段话写完成判定标准 (可被 LLM 评)。
      ``None`` 表示由 user/agent 主观判断 (legacy behaviour)。
    * ``deadline`` — Unix 时间戳；超时 GoalGroomingCycle 会重排或
      drop。``None`` 表示无 deadline。
    * ``parent_goal_id`` — 父 goal id；HTN 分解出来的 sub-goal
      用这个串成树。``None`` 是顶层 goal。
    * ``sub_goal_ids`` — 直接子 goal id 列表 (HTN 分解结果)。
    * ``task_ids`` — 已绑定到 TaskScheduler 的 Task id 列表。
    * ``assigned_agent`` — 哪个 sub-agent 负责此 goal。``"main"``
      表示主 agent；其他值由 MultiAgentManager 解析。
    * ``estimated_cost_usd`` — HTN 估计的 LLM 成本上限 (美元)。
      ``None`` 表示未估计。
    * ``updated_at`` — 最后状态变化时间。GoalGroomingCycle 用它
      判断 stale。
    * ``status`` 取值扩展：
      ``"active"`` (legacy) | ``"completed"`` | ``"abandoned"``
      | ``"blocked"`` | ``"needs_replan"`` | ``"in_progress"``。
    """

    id: str
    description: str
    priority: int = 5  # 1-10, 10 最高
    source: str = "user"  # "user" | "system" | "inferred"
    created_at: float = field(default_factory=time.time)
    status: str = "active"
    # ── R2 additions ───────────────────────────────────────────
    success_criteria: str | None = None
    deadline: float | None = None
    parent_goal_id: str | None = None
    sub_goal_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    assigned_agent: str = "main"
    estimated_cost_usd: float | None = None
    updated_at: float = field(default_factory=time.time)


@dataclass
class AttentionFocus:
    """注意力焦点。"""

    percept_id: str
    content: str
    salience_score: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class SalienceWeights:
    """显著性计算权重。"""

    urgency: float = 0.35
    relevance: float = 0.35
    novelty: float = 0.20
    fatigue: float = 0.10


@dataclass
class CognitiveState:
    """系统的持续认知状态。

    取代 AgentLoop 中分散的隐式状态：
    - _histories → 仍由 AgentLoop 管理（session 级别），但 CognitiveState 记录元数据
    - _frozen_prompts → 由 PersonaAssembler 管理
    - _cancel_events → 由 CognitiveState.cancel_events 统一管理
    - _curriculum_hint_fired → 由 CognitiveState.session_flags 管理
    """

    # 当前目标
    current_goals: list[Goal] = field(default_factory=list)

    # 注意力焦点（有限容量窗口，7±2）
    attention_focus: list[AttentionFocus] = field(default_factory=list)
    attention_capacity: int = 7
    salience_threshold: float = 0.3
    salience_weights: SalienceWeights = field(default_factory=SalienceWeights)

    # 疲劳度（用于显著性计算）
    fatigue: dict[str, float] = field(default_factory=dict)
    fatigue_decay: float = 0.1  # 每轮衰减

    # 会话级标记
    session_flags: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 例如: {"session_abc": {"curriculum_hint_fired": True}}

    # 取消事件（替代 AgentLoop._cancel_events）
    cancel_events: dict[str, Any] = field(default_factory=dict)

    # 活跃计划
    active_plans: list[dict[str, Any]] = field(default_factory=list)

    # 待执行动作
    pending_actions: list[dict[str, Any]] = field(default_factory=list)

    # 最后保存时间
    last_saved: float = field(default_factory=time.time)

    # Jarvisification Phase 4: optional embedder for semantic
    # salience computation.  When wired, compute_salience uses
    # cosine similarity between the percept embedding and the
    # current-goal embeddings to derive relevance.
    _embedder: Any = field(default=None, repr=False)

    def set_embedder(self, embedder: Any) -> None:
        """Attach an embedder for semantic relevance."""
        self._embedder = embedder

    async def compute_salience(
        self,
        percept_id: str,
        content: str,
        *,
        urgency: float = 0.5,
        relevance: float | None = None,
        novelty: float = 0.5,
    ) -> float:
        """计算感知事件的显著性分数。

        salience = w1 * urgency + w2 * relevance + w3 * novelty - w4 * fatigue

        Phase 4: when an embedder is wired and ``relevance`` is not
        explicitly supplied, relevance is computed as the max cosine
        similarity between the percept embedding and the embeddings of
        all active goals.
        """
        w = self.salience_weights
        fatigue_score = self.fatigue.get(percept_id, 0.0)

        # Compute relevance via embedding when embedder is available
        # and caller didn't override.
        if relevance is None and self._embedder is not None:
            try:
                relevance = await self._semantic_relevance(content)
            except Exception:
                relevance = 0.5
        elif relevance is None:
            relevance = 0.5

        score = (
            w.urgency * urgency
            + w.relevance * relevance
            + w.novelty * novelty
            - w.fatigue * fatigue_score
        )
        return max(0.0, min(1.0, score))

    async def _semantic_relevance(self, content: str) -> float:
        """Cosine-similarity based relevance against active goals."""
        if not self.current_goals or self._embedder is None:
            return 0.5
        texts = [content] + [g.description for g in self.current_goals if g.status == "active"]
        embeddings = await self._embedder.embed(texts)
        if not embeddings or len(embeddings) < 2:
            return 0.5
        percept_vec = embeddings[0]
        goal_vecs = embeddings[1:]
        max_sim = 0.0
        for gv in goal_vecs:
            sim = _cosine_similarity(percept_vec, gv)
            if sim > max_sim:
                max_sim = sim
        return max_sim

    def add_focus(self, focus: AttentionFocus) -> None:
        """添加注意力焦点。如超出容量，淘汰最低显著性的。"""
        self.attention_focus.append(focus)
        if len(self.attention_focus) > self.attention_capacity:
            self.attention_focus.sort(key=lambda f: f.salience_score)
            evicted = self.attention_focus.pop(0)
            # 增加被驱逐项目的疲劳度
            self.fatigue[evicted.percept_id] = (
                self.fatigue.get(evicted.percept_id, 0.0) + 1.0
            )
        # 衰减所有疲劳度
        for pid in list(self.fatigue.keys()):
            self.fatigue[pid] = max(0.0, self.fatigue[pid] - self.fatigue_decay)

    def get_cancel_event(self, session_id: str) -> Any:
        """获取或创建会话的取消事件。"""
        from asyncio import Event

        if session_id not in self.cancel_events:
            self.cancel_events[session_id] = Event()
        return self.cancel_events[session_id]

    def set_cancelled(self, session_id: str) -> None:
        """标记会话为已取消。"""
        ev = self.get_cancel_event(session_id)
        ev.set()

    def clear_cancelled(self, session_id: str) -> None:
        """清除会话的取消状态。"""
        ev = self.get_cancel_event(session_id)
        ev.clear()

    def is_cancelled(self, session_id: str) -> bool:
        """检查会话是否已取消。"""
        ev = self.get_cancel_event(session_id)
        return ev.is_set()

    def add_goal(self, goal: Goal) -> None:
        """添加目标。"""
        self.current_goals.append(goal)
        self.current_goals.sort(key=lambda g: -g.priority)

    def complete_goal(self, goal_id: str) -> bool:
        """标记目标完成。"""
        for goal in self.current_goals:
            if goal.id == goal_id:
                goal.status = "completed"
                return True
        return False

    def get_session_flag(self, session_id: str, key: str, default: Any = None) -> Any:
        """获取会话标记。"""
        return self.session_flags.get(session_id, {}).get(key, default)

    def set_session_flag(self, session_id: str, key: str, value: Any) -> None:
        """设置会话标记。"""
        if session_id not in self.session_flags:
            self.session_flags[session_id] = {}
        self.session_flags[session_id][key] = value

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "current_goals": [
                {
                    "id": g.id,
                    "description": g.description,
                    "priority": g.priority,
                    "source": g.source,
                    "created_at": g.created_at,
                    "status": g.status,
                }
                for g in self.current_goals
            ],
            "attention_focus": [
                {
                    "percept_id": f.percept_id,
                    "content": f.content,
                    "salience_score": f.salience_score,
                    "timestamp": f.timestamp,
                }
                for f in self.attention_focus
            ],
            "fatigue": dict(self.fatigue),
            "last_saved": self.last_saved,
            "attention_capacity": self.attention_capacity,
            "salience_threshold": self.salience_threshold,
            "salience_weights": {
                "urgency": self.salience_weights.urgency,
                "relevance": self.salience_weights.relevance,
                "novelty": self.salience_weights.novelty,
                "fatigue": self.salience_weights.fatigue,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CognitiveState":
        """从字典反序列化。"""
        w_data = data.get("salience_weights", {})
        weights = SalienceWeights(
            urgency=w_data.get("urgency", 0.35),
            relevance=w_data.get("relevance", 0.35),
            novelty=w_data.get("novelty", 0.20),
            fatigue=w_data.get("fatigue", 0.10),
        )
        state = cls(
            fatigue=data.get("fatigue", {}),
            last_saved=data.get("last_saved", time.time()),
        )
        state.attention_capacity = data.get("attention_capacity", 7)
        state.salience_threshold = data.get("salience_threshold", 0.3)
        state.salience_weights = weights
        for g in data.get("current_goals", []):
            state.current_goals.append(
                Goal(
                    id=g["id"],
                    description=g["description"],
                    priority=g["priority"],
                    source=g.get("source", "user"),
                    created_at=g.get("created_at", 0.0),
                    status=g.get("status", "active"),
                )
            )
        for f in data.get("attention_focus", []):
            state.attention_focus.append(
                AttentionFocus(
                    percept_id=f["percept_id"],
                    content=f["content"],
                    salience_score=f["salience_score"],
                    timestamp=f.get("timestamp", 0.0),
                )
            )
        return state



def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))