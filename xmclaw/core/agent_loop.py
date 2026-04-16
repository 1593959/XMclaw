"""Core agent loop: think -> act -> observe -> repeat."""
import json
import re
from typing import AsyncIterator

from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.core.prompt_builder import PromptBuilder
from xmclaw.core.cost_tracker import CostTracker
from xmclaw.core.reflection import ReflectionEngine
from xmclaw.genes.manager import GeneManager
from xmclaw.utils.log import logger


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
        self.pending_question: str | None = None
        self.pending_answer: str | None = None
        self.reflection = ReflectionEngine(llm_router, memory)
        self._turn_history: list[dict] = []
        self._load_markdown_configs()

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

    async def run(self, user_input: str) -> AsyncIterator[str]:
        """Run the agent loop, yielding JSON-encoded events."""
        logger.info("agent_loop_start", agent_id=self.agent_id, input=user_input[:200])

        # Handle plan mode toggle from input
        if user_input.startswith("[PLAN MODE]"):
            self.plan_mode = True
            user_input = user_input.replace("[PLAN MODE]", "").strip()
            yield json.dumps({"type": "state", "state": "PLANNING", "thought": "计划模式已开启，正在构建执行计划..."})
        elif user_input.startswith("[RESUME]"):
            # Resume from ask_user with answer
            self.pending_answer = user_input.replace("[RESUME]", "").strip()
            user_input = self.pending_answer
            self.pending_question = None
            # If resuming from plan mode, disable it so we execute the plan
            was_plan_mode = self.plan_mode
            self.plan_mode = False
            yield json.dumps({"type": "state", "state": "THINKING", "thought": "继续处理用户回复..."})
            # For plan mode resume, append the plan context to messages so the agent knows what to execute
            if was_plan_mode:
                # Rebuild context with the original plan from the last turn
                last_turn = self._turn_history[-1] if self._turn_history else None
                if last_turn:
                    plan_text = last_turn.get("assistant", "")
                    user_input = f"[继续执行以下计划]\n{plan_text}\n\n用户确认：{user_input}"
        else:
            # Normal message: clear any pending question state
            self.pending_question = None
            self.pending_answer = None
            yield json.dumps({"type": "state", "state": "THINKING", "thought": "分析请求中..."})

        context = await self.memory.load_context(self.agent_id, user_input)
        context["tool_descriptions"] = self._build_tool_descriptions()
        context["matched_genes"] = self.gene_manager.match(user_input)
        messages = self.builder.build(user_input, context, plan_mode=self.plan_mode)

        turn_count = 0
        while turn_count < self.max_turns:
            turn_count += 1

            # Stream thinking
            full_response = ""
            async for chunk in self.llm.stream(messages):
                full_response += chunk
                yield json.dumps({"type": "chunk", "content": chunk})

            # Parse tool calls
            tool_calls = self._extract_tool_calls(full_response)

            turn_data = {"user": user_input, "assistant": full_response, "tool_calls": tool_calls}
            self._turn_history.append(turn_data)
            await self.memory.save_turn(self.agent_id, user_input, full_response, tool_calls)

            if not tool_calls:
                break

            # Plan mode: pause before executing tools, wait for user confirmation
            if self.plan_mode and turn_count == 1:
                self.pending_question = f"计划已生成，是否执行？\n\n计划内容：\n{full_response}"
                yield json.dumps({"type": "ask_user", "question": self.pending_question})
                yield json.dumps({"type": "state", "state": "WAITING", "thought": "等待用户确认计划..."})
                return

            # Execute tools
            observations = []
            for call in tool_calls:
                tool_name = call["name"]
                args = call.get("arguments", {})

                yield json.dumps({"type": "state", "state": "TOOL_CALL", "thought": f"Using {tool_name}..."})
                yield json.dumps({"type": "tool_call", "tool": tool_name, "args": args})

                result = await self.tools.execute(tool_name, args)

                # Handle ask_user special pause
                if tool_name == "ask_user" and result.startswith("[ASK_USER]"):
                    question = result.replace("[ASK_USER]", "").strip()
                    self.pending_question = question
                    yield json.dumps({"type": "ask_user", "question": question})
                    yield json.dumps({"type": "state", "state": "WAITING", "thought": "等待用户回复..."})
                    return

                observations.append({"tool": tool_name, "result": result})
                yield json.dumps({"type": "tool_result", "tool": tool_name, "result": result})

                # Detect self-modification
                await self._detect_self_mod(call, result)

            # Append to messages for next turn
            messages.append({"role": "assistant", "content": full_response})
            messages.append({"role": "user", "content": self._format_observations(observations)})

        # Cost summary
        cost_info = self.cost_tracker.estimate(messages)
        yield json.dumps({"type": "cost", "tokens": cost_info.get("tokens", 0), "cost": cost_info.get("cost", 0)})

        # Reflection
        try:
            reflection = await self.reflection.reflect(self.agent_id, self._turn_history)
            if reflection:
                yield json.dumps({"type": "reflection", "data": reflection})
        except Exception as e:
            logger.error("reflection_failed", agent_id=self.agent_id, error=str(e))

        self._turn_history.clear()
        yield json.dumps({"type": "done"})
        logger.info("agent_loop_end", agent_id=self.agent_id, turns=turn_count)

    def _extract_tool_calls(self, text: str) -> list[dict]:
        """Extract tool calls from LLM response."""
        calls = []
        pattern = r"<function>(.*?)</function>\s*<arguments>(.*?)</arguments>"
        for match in re.finditer(pattern, text, re.DOTALL):
            name = match.group(1).strip()
            args_raw = match.group(2).strip()
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {"raw": args_raw}
            calls.append({"name": name, "arguments": args})
        return calls

    def _format_observations(self, observations: list[dict]) -> str:
        lines = ["工具执行结果："]
        for obs in observations:
            lines.append(f"- {obs['tool']}: {obs['result']}")
        return "\n".join(lines)

    def _build_tool_descriptions(self) -> str:
        """Build a formatted string of all available tools and their parameters."""
        lines = []
        for tool in self.tools._tools.values():
            params = ", ".join([f"{k}: {v.get('type', 'any')}" for k, v in tool.parameters.items()])
            lines.append(f"- {tool.name}: {tool.description} Parameters: ({params})")
        return "\n".join(lines)

    async def _detect_self_mod(self, call: dict, result: any) -> None:
        """Detect if the agent is modifying its own source code."""
        import json
        name = call.get("name", "")
        args = call.get("arguments", {})

        if name in ("file_write", "file_edit", "file_read"):
            path = args.get("file_path", args.get("path", ""))
            if "xmclaw" in path.lower() or "XMclaw" in path:
                action = "read" if name == "file_read" else "modify"
                logger.info("self_mod_detected", agent_id=self.agent_id, file=path, action=action)
