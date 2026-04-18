"""Core agent loop: Think → Analyze → Gather → Plan → Execute → Reflect."""
import asyncio
import json
import time as _time
from typing import AsyncIterator

from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.core.prompt_builder import PromptBuilder
from xmclaw.core.cost_tracker import CostTracker
from xmclaw.core.reflection import ReflectionEngine
from xmclaw.genes.manager import GeneManager
from xmclaw.core.event_bus import Event, EventType, get_event_bus
from xmclaw.utils.log import logger

# ── Five-Stage Cognition Pipeline ────────────────────────────────────────────
from xmclaw.core.task_classifier import TaskClassifier, TaskType, Complexity
from xmclaw.core.info_gather import InfoGatherer
from xmclaw.core.task_planner import TaskPlanner
from xmclaw.core.skill_matcher import SkillMatcher
from xmclaw.evolution.engine import EvolutionEngine


class AgentLoop:
    def __init__(self, agent_id: str, llm_router: LLMRouter, tools: ToolRegistry, memory: MemoryManager):
        self.agent_id = agent_id
        self.llm = llm_router
        self.tools = tools
        self.memory = memory
        self.builder = PromptBuilder()
        self.cost_tracker = CostTracker()
        self.gene_manager = GeneManager(agent_id)
        self.max_turns = 50
        self.plan_mode = False
        self._plan_approved = False  # set to True after user approves a plan
        self.pending_question: str | None = None
        self.pending_answer: str | None = None
        self.reflection = ReflectionEngine(llm_router, memory)
        self._event_bus = get_event_bus()
        self._turn_history: list[dict] = []
        self._load_markdown_configs()

        # ── Five-Stage Cognition Pipeline ─────────────────────────────────────
        self.classifier = TaskClassifier(llm_router)
        self.gatherer = InfoGatherer(llm_router, memory, agent_id=agent_id)
        self.planner = TaskPlanner(llm_router)
        self.skill_matcher = SkillMatcher(memory)

        # ── Real-time evolution: tool usage pattern tracker ────────────────────
        self._tool_patterns: dict[str, int] = {}
        # Load thresholds from config (with defaults for backward compatibility)
        try:
            from xmclaw.daemon.config import DaemonConfig
            config = DaemonConfig.load()
            self._pattern_thresholds = config.evolution.get("pattern_thresholds", {})
            self._tool_thresholds = config.evolution.get("tool_specific_thresholds", {})
            self._pattern_threshold = self._pattern_thresholds.get("tool_usage_min_count", 2)
        except Exception:
            self._pattern_thresholds = {}
            self._tool_thresholds = {}
            self._pattern_threshold = 2  # Lower default for more sensitivity
        self._evolution_running = False   # prevent concurrent evolution cycles

        self._event_bus.subscribe(
            EventType.PATTERN_THRESHOLD,
            self._on_pattern_threshold,
        )

    async def _on_pattern_threshold(self, event: Event) -> None:
        """Handle pattern:threshold_reached event — immediately trigger evolution."""
        tool = event.payload.get("tool", "")
        count = event.payload.get("count", 0)
        if not tool:
            return
        try:
            await self._event_bus.publish(Event(
                event_type=EventType.EVOLUTION_TRIGGER,
                source=self.agent_id,
                payload={
                    "trigger": "pattern_threshold",
                    "tool": tool,
                    "count": count,
                },
            ))
            from xmclaw.evolution.engine import EvolutionEngine
            engine = EvolutionEngine(agent_id=self.agent_id,
                                    memory=self.memory)
            await engine.initialize()
            result = await engine.run_cycle()
            if result and result.get("actions"):
                await self._event_bus.publish(Event(
                    event_type=EventType.EVOLUTION_NOTIFY,
                    source=self.agent_id,
                    payload=result,
                ))
                logger.info("pattern_evolution_completed",
                             tool=tool, count=count, actions=len(result.get("actions", [])))
        except Exception as e:
            logger.warning("pattern_evolution_failed", tool=tool, error=str(e))

    def _load_markdown_configs(self) -> None:
        """Load SOUL.md, PROFILE.md, AGENTS.md from agent directory."""
        self._soul = ""
        self._profile = ""
        self._agents = ""
        try:
            from xmclaw.utils.paths import get_agent_dir
            agent_dir = get_agent_dir(self.agent_id)
            if agent_dir is None:
                return
            for fname, attr in [
                ("SOUL.md", "_soul"),
                ("PROFILE.md", "_profile"),
                ("AGENTS.md", "_agents"),
            ]:
                p = agent_dir / fname
                if p.exists():
                    try:
                        setattr(self, attr, p.read_text(encoding="utf-8"))
                    except Exception:
                        pass
        except Exception:
            pass

    @staticmethod
    def _wrap_parameters(raw: dict) -> dict:
        """Wrap a flat property dict into a valid JSON Schema object.

        Tool definitions store parameters as ``{prop_name: {type, description}}``.
        LLM APIs require ``{type: 'object', properties: {...}, required: [...]}``.
        """
        if not raw:
            return {"type": "object", "properties": {}}
        if "type" in raw and raw["type"] == "object":
            return raw  # already wrapped
        required = [
            k for k, v in raw.items()
            if not str(v.get("description", "")).lower().startswith("optional")
        ]
        return {"type": "object", "properties": raw, "required": required}

    def _get_tools_for_llm(self) -> list[dict]:
        """Build tool list for LLM in standard JSON Schema format."""
        tools = []
        for tool in self.tools._tools.values():
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "parameters": self._wrap_parameters(tool.parameters),
            })
        return tools

    async def run(self, user_input: str) -> AsyncIterator[str]:
        """Run the agent loop, yielding JSON-encoded events."""
        logger.info("agent_loop_start", agent_id=self.agent_id, input=user_input[:200])

        # Reset per-session state
        self._turn_history.clear()
        self._stages_done: set[str] = set()   # guards against stage re-run on asend() resume

        # Handle special message types
        if user_input.startswith("[PLAN MODE]"):
            self.plan_mode = True
            user_input = user_input.replace("[PLAN MODE]", "").strip()
        elif user_input.startswith("[RESUME]"):
            self.pending_answer = user_input.replace("[RESUME]", "").strip()
            user_input = self.pending_answer
            self.pending_question = None
        elif user_input.startswith("[PLAN APPROVE]"):
            self._plan_approved = True
            user_input = user_input.replace("[PLAN APPROVE]", "").strip()
            self.pending_question = None
        else:
            self.pending_question = None
            self.pending_answer = None

        # Publish user message event
        await self._event_bus.publish(Event(
            event_type=EventType.USER_MESSAGE,
            source=self.agent_id,
            payload={"content": user_input[:500]},
        ))

        # ══════════════════════════════════════════════════════════════════════════
        #  FIVE-STAGE COGNITION PIPELINE
        #  Each stage checks _stages_done so that if the async generator is paused
        #  by an ask_user yield and resumed via asend(), completed stages do NOT
        #  re-run — the generator resumes from the exact yield point.
        # ══════════════════════════════════════════════════════════════════════════

        # ── Stage 1: Task Classification ─────────────────────────────────────────
        if "stage1" in self._stages_done:
            # Already completed — restore from cached result
            task_profile = self._task_profile
        else:
            yield json.dumps({"type": "stage", "stage": "analyze",
                               "label": "🔍 任务分析",
                               "desc": "正在分析任务类型与复杂度..."})
            task_profile = await self.classifier.classify(user_input)
            self._task_profile = task_profile   # cache for resume
            yield json.dumps({
                "type": "stage", "stage": "analyze_done",
                "label": "✅ 任务分析完成",
                "desc": f"类型: {task_profile['type'].value} | 复杂度: {task_profile['complexity'].value}",
                "data": {
                    "type": task_profile["type"].value,
                    "complexity": task_profile["complexity"].value,
                    "capabilities": task_profile["capabilities_needed"],
                    "reasoning": task_profile["reasoning"],
                }
            })
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_THINKING,
                source=self.agent_id,
                payload={"stage": "analyze", "profile": {
                    "type": task_profile["type"].value,
                    "complexity": task_profile["complexity"].value,
                    "capabilities_needed": task_profile["capabilities_needed"],
                    "reasoning": task_profile["reasoning"],
                }},
            ))
            logger.info("stage1_analyze_complete",
                         type=task_profile["type"].value, complexity=task_profile["complexity"].value)
            self._stages_done.add("stage1")

        # ── Stage 2: Information Gathering ──────────────────────────────────────
        if "stage2" in self._stages_done:
            gathered = self._gathered
            gathered_text = self._gathered_text
        else:
            yield json.dumps({"type": "stage", "stage": "gather",
                               "label": "📡 信息收集",
                               "desc": "搜索记忆、经验、网络..."})
            gathered = await self.gatherer.gather(
                user_input, task_profile["type"], task_profile["capabilities_needed"]
            )
            self._gathered = gathered
            gathered_text = self.gatherer.format_for_prompt(gathered)
            self._gathered_text = gathered_text
            yield json.dumps({
                "type": "stage", "stage": "gather_done",
                "label": "✅ 信息收集完成",
                "desc": f"记忆:{len(gathered['memories'])} | 经验:{len(gathered['insights'])} | 网页:{len(gathered['web_results'])}",
                "data": {
                    "memories": gathered["memories"],
                    "insights": gathered["insights"],
                    "web_results": gathered["web_results"],
                }
            })
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_THINKING,
                source=self.agent_id,
                payload={"stage": "gather", "summary": gathered["reasoning"]},
            ))
            logger.info("stage2_gather_complete",
                         memories=len(gathered["memories"]),
                         insights=len(gathered["insights"]),
                         web=len(gathered["web_results"]))
            self._stages_done.add("stage2")

        # ── Stage 3: Task Planning (medium or high complexity) ──────────────────
        planning = task_profile["complexity"] in (Complexity.MEDIUM, Complexity.HIGH)
        if planning and "stage3" not in self._stages_done:
            yield json.dumps({"type": "stage", "stage": "plan",
                               "label": "🗺 任务规划",
                               "desc": "正在拆解任务步骤..."})
            execution_plan = await self.planner.plan(user_input, task_profile, gathered_text)
            plan_text = self.planner.format_plan_for_prompt(execution_plan)
            self._execution_plan = execution_plan
            self._plan_text = plan_text
            yield json.dumps({
                "type": "stage", "stage": "plan_done",
                "label": "✅ 规划完成",
                "desc": f"共 {execution_plan['estimated_steps']} 个步骤" +
                        (" (需确认)" if execution_plan["needs_confirmation"] else ""),
                "data": {
                    "steps": execution_plan["steps"],
                    "estimated_steps": execution_plan["estimated_steps"],
                    "needs_confirmation": execution_plan["needs_confirmation"],
                    "reasoning": execution_plan["reasoning"],
                }
            })
            # If plan requires confirmation, yield ask_user and pause here
            if execution_plan["needs_confirmation"]:
                self.pending_question = f"【执行计划】\n{plan_text}\n\n是否按此计划执行？"
                yield json.dumps({"type": "ask_user", "question": self.pending_question})
                yield json.dumps({"type": "state", "state": "WAITING", "thought": "等待用户确认计划..."})
                self._stages_done.add("stage3_asked")  # mark so yield doesn't repeat on resume
                sent = yield   # ← generator pauses here; resumes with user's answer
                self.pending_answer = sent if sent else ""
                self.pending_question = None
                if not self.pending_answer.strip():
                    yield json.dumps({"type": "chunk", "content": "已取消执行。"})
                    yield json.dumps({"type": "done"})
                    return
            logger.info("stage3_plan_complete",
                         steps=execution_plan["estimated_steps"],
                         needs_confirm=execution_plan["needs_confirmation"])
            self._stages_done.add("stage3")
        elif not planning:
            self._execution_plan = None
            self._plan_text = ""
            logger.info("stage3_plan_skipped", complexity=task_profile["complexity"].value)
        elif "stage3" in self._stages_done:
            # Resume path: restore cached plan results
            execution_plan = self._execution_plan
            plan_text = self._plan_text

        # ── Stage 4: Skill Matching ─────────────────────────────────────────────
        if "stage4" in self._stages_done:
            skill_result = self._skill_result
            skill_text = self._skill_text
        else:
            yield json.dumps({"type": "stage", "stage": "skill_match",
                               "label": "⚡ 技能匹配",
                               "desc": "搜索相关技能并执行..."})
            skill_result = await self.skill_matcher.match_and_execute(
                user_input, task_profile["type"],
                task_profile["capabilities_needed"], gathered
            )
            self._skill_result = skill_result
            skill_text = self.skill_matcher.format_skill_results(skill_result)
            self._skill_text = skill_text
            if skill_result["auto_executed"]:
                yield json.dumps({
                    "type": "stage", "stage": "skill_match_done",
                    "label": "✅ 技能执行完成",
                    "desc": f"已自动执行 {len(skill_result['auto_executed'])} 个技能",
                    "data": {
                        "matched": [{"name": s["name"], "score": s["score"]} for s in skill_result["matched"]],
                        "auto_executed": skill_result["auto_executed"],
                        "failed": skill_result["failed"],
                    }
                })
            else:
                yield json.dumps({
                    "type": "stage", "stage": "skill_match_done",
                    "label": "⚖ 技能匹配完成",
                    "desc": f"匹配到 {len(skill_result['matched'])} 个相关技能（无可自动执行）",
                    "data": {
                        "matched": [{"name": s["name"], "score": s["score"]} for s in skill_result["matched"]],
                        "auto_executed": [],
                        "failed": [],
                    }
                })
            logger.info("stage4_skill_match_complete",
                         matched=len(skill_result["matched"]),
                         executed=len(skill_result["auto_executed"]))
            self._stages_done.add("stage4")

        # ── Stage 5: Prompt Building ────────────────────────────────────────────
        if "stage5" not in self._stages_done:
            history = await self.memory.sessions.get_recent(self.agent_id, limit=10) if self.memory.sessions else []
            context = {
                "history": history,
                "memories": gathered["memories"],
                "insights": gathered["insights"],
                "tool_descriptions": self._build_tool_descriptions(),
                "matched_genes": self.gene_manager.match(user_input),
                "gathered_info": gathered_text,
                "execution_plan": plan_text,
                "skill_results": skill_text,
                "task_profile": {
                    "type": task_profile["type"].value,
                    "complexity": task_profile["complexity"].value,
                    "reasoning": task_profile["reasoning"],
                },
            }
            messages = self.builder.build(user_input, context, plan_mode=self.plan_mode)
            self._messages = messages   # cache for resume
            self._stages_done.add("stage5")
        else:
            messages = self._messages   # resume path: reuse cached messages

        # ══════════════════════════════════════════════════════════════════════════
        #  MAIN EXECUTION LOOP  (think → act → observe → repeat)
        # ══════════════════════════════════════════════════════════════════════════
        yield json.dumps({"type": "state", "state": "THINKING",
                           "thought": f"执行中（类型:{task_profile['type'].value}）..."})

        turn_count = 0
        while turn_count < self.max_turns:
            turn_count += 1

            # Stream thinking with tool calling support
            full_response = ""
            tool_calls = []
            current_tool = None
            current_tool_input = ""
            in_tool_call = False

            async for event_str in self.llm.stream(messages, tools=self._get_tools_for_llm()):
                # Parse the event
                try:
                    event = json.loads(event_str)
                except json.JSONDecodeError:
                    # Plain text chunk
                    full_response += event_str
                    yield json.dumps({"type": "chunk", "content": event_str})
                    continue

                event_type = event.get("type", "")

                if event_type == "text":
                    # Text chunk
                    content = event.get("content", "")
                    full_response += content
                    yield json.dumps({"type": "chunk", "content": content})

                elif event_type == "tool_call_start":
                    # Start of a tool call
                    current_tool = {
                        "id": event.get("id"),
                        "name": event.get("name"),
                        "input": {}
                    }
                    in_tool_call = True
                    current_tool_input = ""

                elif event_type == "tool_call_input":
                    # Tool input delta
                    if current_tool:
                        current_tool_input += event.get("input_delta", "")
                        # Try to parse as JSON
                        try:
                            current_tool["input"] = json.loads(current_tool_input)
                        except json.JSONDecodeError:
                            pass  # Keep accumulating

                elif event_type == "tool_call_end":
                    if current_tool:
                        # Final attempt to parse accumulated JSON if not yet parsed
                        if current_tool_input and not current_tool.get("input"):
                            try:
                                current_tool["input"] = json.loads(current_tool_input)
                            except json.JSONDecodeError:
                                logger.warning("tool_call_input_parse_failed",
                                               tool=current_tool.get("name"),
                                               raw=current_tool_input[:200])
                                current_tool["input"] = {}
                        tool_calls.append(current_tool)
                        current_tool = None
                        in_tool_call = False

                elif event_type == "error":
                    error_msg = event.get("content", "Unknown error")
                    yield json.dumps({"type": "error", "content": error_msg})
                    break

            turn_data = {
                "user": user_input,
                "assistant": full_response,
                "tool_calls": tool_calls,
                "tool_observations": observations,  # per-tool results
                "turn": turn_count,
            }
            self._turn_history.append(turn_data)
            await self.memory.save_turn(self.agent_id, user_input, full_response, tool_calls)

            # Publish agent message event
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_MESSAGE,
                source=self.agent_id,
                payload={"content": full_response[:500], "tool_calls": len(tool_calls)},
            ))

            if not tool_calls:
                break

            # Execute tools
            observations = []
            for call in tool_calls:
                tool_name = call["name"]
                args = call.get("input", {})

                # Emit enhanced tool call event with metadata
                tool_start_time = _time.time()
                import json as _json
                yield _json.dumps({
                    "type": "tool_start",
                    "tool": tool_name,
                    "args": args,
                    "call_id": call.get("id", f"call_{tool_name}_{tool_start_time}"),
                })
                yield json.dumps({"type": "state", "state": "TOOL_CALL", "thought": f"Using {tool_name}...", "tool": tool_name})
                yield json.dumps({"type": "tool_call", "tool": tool_name, "args": args})

                result = await self.tools.execute(tool_name, args)
                tool_duration = round(_time.time() - tool_start_time, 2)

                # ── Real-time pattern tracking ────────────────────────────────────
                # Every tool call is recorded to detect when a pattern should
                # trigger immediate skill/gene generation.
                self._tool_patterns[tool_name] = self._tool_patterns.get(tool_name, 0) + 1
                # Use tool-specific threshold if configured, otherwise use default
                effective_threshold = self._tool_thresholds.get(tool_name, self._pattern_threshold)
                await self._event_bus.publish(Event(
                    event_type=EventType.TOOL_CALLED,
                    source=self.agent_id,
                    payload={
                        "tool": tool_name,
                        "count": self._tool_patterns[tool_name],
                        "threshold": self._pattern_threshold,
                        "effective_threshold": effective_threshold,
                        "is_tool_specific": tool_name in self._tool_thresholds,
                        "args": args,
                    },
                ))
                # Publish pattern_detected event when tool-specific or default threshold is reached
                if self._tool_patterns[tool_name] >= effective_threshold:
                    await self._event_bus.publish(Event(
                        event_type=EventType.PATTERN_THRESHOLD,
                        source=self.agent_id,
                        payload={
                            "tool": tool_name,
                            "count": self._tool_patterns[tool_name],
                            "threshold": effective_threshold,
                            "is_tool_specific": tool_name in self._tool_thresholds,
                        },
                    ))
                    logger.info("tool_pattern_threshold_reached",
                                 tool=tool_name, count=self._tool_patterns[tool_name])

                # Handle ask_user special pause
                if tool_name == "ask_user" and str(result).startswith("[ASK_USER]"):
                    question = str(result).replace("[ASK_USER]", "").strip()
                    self.pending_question = question
                    yield json.dumps({"type": "ask_user", "question": question})
                    yield json.dumps({"type": "state", "state": "WAITING", "thought": "等待用户回复..."})
                    # Use asend() to receive the user's answer without re-executing turns.
                    # server.py calls generator.asend(answer) after user responds.
                    # The result of yield becomes the sent value, so:
                    sent = yield  # receives the user's answer string
                    self.pending_answer = sent if sent else ""
                    self.pending_question = None
                    # Continue to next iteration with the user's answer as the tool result
                    observations.append({
                        "tool": tool_name,
                        "result": sent or "[no answer]",
                        "duration": 0.0,
                    })
                    yield json.dumps({
                        "type": "tool_result",
                        "tool": tool_name,
                        "result": sent or "[no answer]",
                        "duration": 0.0,
                        "call_id": call.get("id", ""),
                    })
                    # Don't append to messages here — the user answer is the tool result
                    tool_result_content = [{
                        "type": "tool_result",
                        "tool_use_id": call.get("id") or f"toolu_{tool_name}",
                        "content": sent or "[no answer]",
                    }]
                    messages.append({"role": "user", "content": tool_result_content})
                    continue  # ← do NOT fall through to normal tool_result handling

                observations.append({"tool": tool_name, "result": result, "duration": tool_duration})
                yield json.dumps({
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": result,
                    "duration": tool_duration,
                    "call_id": call.get("id", ""),
                })

                # Detect self-modification (file ops) and emit to UI
                file_event = await self._detect_self_mod(call, result)
                if file_event:
                    yield json.dumps(file_event)

                await self._event_bus.publish(Event(
                    event_type=EventType.TOOL_RESULT,
                    source=self.agent_id,
                    payload={"tool": tool_name, "result": str(result)[:200]},
                ))

            # Add assistant message with tool_use blocks (Anthropic native format;
            # openai_client normalises these into the OpenAI wire format automatically)
            assistant_content: list[dict] = []
            if full_response:
                assistant_content.append({"type": "text", "text": full_response})
            for call in tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": call.get("id") or f"toolu_{call['name']}",
                    "name": call["name"],
                    "input": call.get("input", {}),
                })
            messages.append({"role": "assistant", "content": assistant_content or full_response})

            # Tool results as user message with tool_result blocks
            tool_result_content: list[dict] = []
            for call, obs in zip(tool_calls, observations):
                tool_result_content.append({
                    "type": "tool_result",
                    "tool_use_id": call.get("id") or f"toolu_{call['name']}",
                    "content": str(obs["result"]),
                })
            messages.append({"role": "user", "content": tool_result_content})

            # Check if we should continue
            yield json.dumps({"type": "state", "state": "THINKING", "thought": "处理工具结果中..."})

        # ── Reflection: SYNCHRONOUS + VISIBLE ────────────────────────────────────
        # Unlike the old fire-and-forget background task, reflection now runs
        # inline and yields its result to the frontend so the user can see it.
        if self._turn_history:
            yield json.dumps({"type": "stage", "stage": "reflect",
                              "label": "🧠 反思总结",
                              "desc": "正在分析本次对话..."})
            try:
                reflection_result = await self.reflection.reflect(self.agent_id, self._turn_history)
                if reflection_result and reflection_result.get("reflection"):
                    yield json.dumps({
                        "type": "stage", "stage": "reflect_done",
                        "label": "✅ 反思完成",
                        "desc": reflection_result["reflection"].get("summary", "反思已完成"),
                        "data": {
                            "summary": reflection_result["reflection"].get("summary", ""),
                            "problems": reflection_result["reflection"].get("problems", []),
                            "lessons": reflection_result["reflection"].get("lessons", []),
                            "improvements": reflection_result["reflection"].get("improvements", []),
                        }
                    })
                    # Publish to EventBus for other subscribers
                    await self._event_bus.publish(Event(
                        event_type=EventType.REFLECTION_COMPLETE,
                        source=self.agent_id,
                        payload={
                            "reflection": reflection_result["reflection"],
                            "improvement": reflection_result.get("improvement", {}),
                        },
                    ))
                    # Trigger immediate evolution after reflection
                    await self._trigger_immediate_evolution(dict(self._tool_patterns))
            except Exception as e:
                logger.warning("inline_reflection_failed", error=str(e))

        yield json.dumps({"type": "done"})

    def _schedule_evolution_only(self) -> None:
        """Fire-and-forget: trigger evolution (pattern-based) in background.

        Reflection itself is now inline (see run()). This method handles
        pure background evolution triggers that run after the response is sent.
        """
        patterns = dict(self._tool_patterns)  # snapshot real-time tool patterns
        if not patterns:
            return

        async def _bg() -> None:
            try:
                await self._trigger_immediate_evolution(patterns)
            except Exception as e:
                logger.warning("background_evolution_failed", agent_id=self.agent_id, error=str(e))

        try:
            asyncio.create_task(_bg())
        except Exception as e:
            logger.warning("evolution_task_create_failed", error=str(e))

    async def _trigger_immediate_evolution(self, patterns: dict[str, int]) -> None:
        """Immediately run a targeted evolution cycle.

        Deduplicated: if a cycle is already running, skip this trigger.
        """
        if self._evolution_running:
            logger.debug("evolution_skipped_already_running")
            return
        self._evolution_running = True
        try:
            # Publish evolution trigger event for frontend
            await self._event_bus.publish(Event(
                event_type=EventType.EVOLUTION_TRIGGER,
                source=self.agent_id,
                payload={
                    "trigger": "conversation_end",
                    "patterns": patterns,
                    "tool_counts": patterns,
                },
            ))

            # Run the evolution engine

            # Use the agent's existing memory manager
            engine = EvolutionEngine(
                agent_id=self.agent_id,
                memory=self.memory,
            )
            await engine.initialize()
            result = await engine.run_cycle()

            if result and result.get("actions"):
                # Publish results as evolution:notify event (frontend can display these)
                await self._event_bus.publish(Event(
                    event_type=EventType.EVOLUTION_NOTIFY,
                    source=self.agent_id,
                    payload=result,
                ))
                logger.info("immediate_evolution_completed",
                             actions=len(result.get("actions", [])), result=result)
        except Exception as e:
            logger.warning("immediate_evolution_failed", agent_id=self.agent_id, error=str(e))
        finally:
            self._evolution_running = False

    def _build_tool_descriptions(self) -> str:
        """Build a formatted string of all available tools and their parameters."""
        lines = []
        for tool in self.tools._tools.values():
            raw = tool.parameters
            props = raw.get("properties", raw) if isinstance(raw, dict) else {}
            params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items() if isinstance(v, dict))
            lines.append(f"- {tool.name}: {tool.description} Parameters: ({params})")
        return "\n".join(lines)

    async def _detect_self_mod(self, call: dict, result: any) -> dict | None:
        """Detect if the agent is modifying files. Returns file_op event dict or None."""
        name = call.get("name", "")
        args = call.get("input", {})

        if name in ("file_write", "file_edit", "file_read"):
            path = args.get("file_path", args.get("path", ""))
            action = "read" if name == "file_read" else "write"
            logger.info("file_op_detected", agent_id=self.agent_id, file=path, action=action)
            return {"type": "file_op", "file": path, "action": action}
        return None
