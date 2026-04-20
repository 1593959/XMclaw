"""Task Planner: decomposes complex tasks into explicit execution steps.

This is Step 3 of the Agent Cognition Pipeline.
For high-complexity tasks, it generates a structured plan BEFORE execution.
"""
import json
from typing import TypedDict

from xmclaw.llm.router import LLMRouter
from xmclaw.core.task_classifier import TaskType, Complexity, TaskProfile
from xmclaw.utils.log import logger


def _field(profile, key: str) -> str:
    """Read a task_profile field as a plain string.

    TaskProfile is a TypedDict (i.e. a real dict at runtime); earlier code
    used attribute access which raised ``'dict' object has no attribute
    'complexity'`` as soon as a medium/high-complexity task hit the planner.

    The value at a key may be a ``str, Enum`` instance (fresh from
    ``TaskClassifier``) or a plain ``str`` (after a round-trip through JSON
    persistence or the resume cache). Because ``TaskType`` / ``Complexity``
    inherit from ``str``, comparing the returned string to ``Complexity.LOW``
    etc. still works either way.
    """
    v = profile[key]
    return v.value if hasattr(v, "value") else str(v)


class PlanStep(TypedDict):
    step: int
    action: str          # what to do
    tool: str             # which tool to use (empty = LLM reasoning)
    reasoning: str        # why this step
    depends_on: list[int]  # step numbers this depends on


class ExecutionPlan(TypedDict):
    steps: list[PlanStep]
    estimated_steps: int
    needs_confirmation: bool
    reasoning: str


PLAN_PROMPT = """\
你是一位任务规划专家。请将以下用户任务分解为清晰、可执行的步骤序列。

任务: {task}
任务类型: {task_type}
复杂度: {complexity}

背景信息:
{context}

请输出 JSON 格式的执行计划（不要 markdown 代码块）:
{{
  "steps": [
    {{
      "step": 1,
      "action": "具体动作描述",
      "tool": "使用的工具名（不需要工具则填空字符串）",
      "reasoning": "为什么需要这一步",
      "depends_on": []
    }}
  ],
  "estimated_steps": 预计总步数,
  "needs_confirmation": true/false,
  "reasoning": "整体规划思路"
}}

规则:
- step 从 1 开始编号
- depends_on 是该步骤依赖的前置步骤编号列表（默认为空列表）
- needs_confirmation: 仅当任务涉及风险操作（删除文件、重写配置等）时才为 true
- 简单任务 1-2 步，复杂任务可 5-10 步
- 每个步骤必须可独立验证
"""


class TaskPlanner:
    """Plans and decomposes complex tasks into executable steps."""

    def __init__(self, llm_router: LLMRouter):
        self.llm = llm_router

    async def plan(self, user_input: str, profile: TaskProfile,
                   context_info: str = "") -> ExecutionPlan:
        """
        Generate an execution plan. Returns immediately for low complexity
        (returns a single-step implicit plan), or calls LLM for high complexity.
        """
        complexity = _field(profile, "complexity")

        # Low complexity: no planning needed, return implicit single step
        if complexity == Complexity.LOW:
            return ExecutionPlan(
                steps=[PlanStep(
                    step=1,
                    action="直接执行",
                    tool="",
                    reasoning="低复杂度任务，无需显式规划",
                    depends_on=[],
                )],
                estimated_steps=1,
                needs_confirmation=False,
                reasoning="低复杂度，直接执行",
            )

        # Medium complexity: generate lightweight plan
        if complexity == Complexity.MEDIUM:
            return await self._llm_plan(user_input, profile, context_info,
                                         lightweight=True)

        # High complexity: full planning
        return await self._llm_plan(user_input, profile, context_info,
                                     lightweight=False)

    async def _llm_plan(self, user_input: str, profile: TaskProfile,
                        context_info: str, lightweight: bool) -> ExecutionPlan:
        """Call LLM to generate an execution plan."""
        prompt = PLAN_PROMPT.format(
            task=user_input,
            task_type=_field(profile, "type"),
            complexity=_field(profile, "complexity"),
            context=context_info or "无可用背景信息",
        )

        messages = [
            {"role": "system",
             "content": "你是一个严格的任务规划引擎。只输出纯 JSON 格式的计划，不要有任何额外文字。"},
            {"role": "user", "content": prompt},
        ]

        try:
            # NOTE: llm.stream() yields JSON event envelopes (see reflection.py
            # for the full story); use .complete() so _extract_json gets the
            # raw model text. Otherwise medium/high-complexity tasks silently
            # fall through to the hand-rolled fallback plan and the LLM
            # branch is effectively dead code.
            response = await self.llm.complete(messages)

            data = self._extract_json(response)
            if data:
                steps = [
                    PlanStep(
                        step=s.get("step", i+1),
                        action=s.get("action", ""),
                        tool=s.get("tool", ""),
                        reasoning=s.get("reasoning", ""),
                        depends_on=s.get("depends_on", []),
                    )
                    for i, s in enumerate(data.get("steps", []))
                ]
                plan = ExecutionPlan(
                    steps=steps,
                    estimated_steps=data.get("estimated_steps", len(steps)),
                    needs_confirmation=data.get("needs_confirmation", False),
                    reasoning=data.get("reasoning", ""),
                )
                logger.info("plan_generated",
                             steps=len(steps),
                             needs_confirm=plan["needs_confirmation"])
                return plan
        except Exception as e:
            logger.warning("plan_generation_failed", error=str(e))

        # Fallback: single implicit step
        return ExecutionPlan(
            steps=[PlanStep(step=1, action="执行任务", tool="", reasoning="规划失败，使用默认执行", depends_on=[])],
            estimated_steps=1,
            needs_confirmation=False,
            reasoning="LLM 规划失败",
        )

    def _extract_json(self, text: str) -> dict | None:
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        import re
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                pass
        return None

    def format_plan_for_prompt(self, plan: ExecutionPlan) -> str:
        """Format a plan into readable text for injection into the prompt."""
        lines = ["【执行计划】"]
        for step in plan["steps"]:
            tool_part = f" (工具: {step['tool']})" if step["tool"] else ""
            dep_part = f" [依赖: {step['depends_on']}]" if step["depends_on"] else ""
            lines.append(f"  {step['step']}. {step['action']}{tool_part}{dep_part}")
            lines.append(f"     → {step['reasoning']}")
        return "\n".join(lines)

    def next_ready_steps(self, plan: ExecutionPlan,
                          completed: set[int]) -> list[PlanStep]:
        """Return steps that are ready to execute (all dependencies met)."""
        ready = []
        for step in plan["steps"]:
            if step["step"] in completed:
                continue
            deps = set(step.get("depends_on", []))
            if deps.issubset(completed):
                ready.append(step)
        return ready
