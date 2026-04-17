"""Core agent loop: think -> act -> observe -> repeat."""
import json
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
        self._event_bus = get_event_bus()
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
        else:
            # Normal message: clear any pending question state
            self.pending_question = None
            self.pending_answer = None
            yield json.dumps({"type": "state", "state": "THINKING", "thought": "分析请求中..."})

        # Publish user message event
        await self._event_bus.publish(Event(
            event_type=EventType.USER_MESSAGE,
            source=self.agent_id,
            payload={"content": user_input[:500]},
        ))

        context = await self.memory.load_context(self.agent_id, user_input)
        context["tool_descriptions"] = self._build_tool_descriptions()
        context["matched_genes"] = self.gene_manager.match(user_input)
        messages = self.builder.build(user_input, context, plan_mode=self.plan_mode)

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
                    # End of tool call
                    if current_tool:
                        tool_calls.append(current_tool)
                        current_tool = None
                        in_tool_call = False

            turn_data = {"user": user_input, "assistant": full_response, "tool_calls": tool_calls}
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
                args = call.get("input", {})

                yield json.dumps({"type": "state", "state": "TOOL_CALL", "thought": f"Using {tool_name}..."})
                yield json.dumps({"type": "tool_call", "tool": tool_name, "args": args})

                await self._event_bus.publish(Event(
                    event_type=EventType.TOOL_CALLED,
                    source=self.agent_id,
                    payload={"tool": tool_name, "args": args},
                ))

                result = await self.tools.execute(tool_name, args)

                # Handle ask_user special pause
                if tool_name == "ask_user" and str(result).startswith("[ASK_USER]"):
                    question = str(result).replace("[ASK_USER]", "").strip()
                    self.pending_question = question
                    yield json.dumps({"type": "ask_user", "question": question})
                    yield json.dumps({"type": "state", "state": "WAITING", "thought": "等待用户回复..."})
                    return

                observations.append({"tool": tool_name, "result": result})
                yield json.dumps({"type": "tool_result", "tool": tool_name, "result": result})

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

        yield json.dumps({"type": "done"})

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
