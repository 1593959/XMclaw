"""Multi-agent handoff — ``transfer_to_<agent>`` tools.

Following OpenAI Agents SDK's handoff pattern (audit 2026-06-11):
each registered agent gets an auto-generated tool named
``transfer_to_<agent_id>`` that, when called, passes the conversation
context to the target agent and returns its response.

The handoff tool is injected into the tool list alongside other tools.
On invocation it:
  1. Serialises the current conversation state
  2. Calls the target agent's ``run_turn`` with context
  3. Returns the target agent's response as the tool result

This enables the triage + specialist pattern:
  - ``transfer_to_code_reviewer`` for code review tasks
  - ``transfer_to_translator`` for translation tasks
  - ``transfer_to_debugger`` for debugging tasks

Usage (factory)::

    from xmclaw.providers.tool.handoff import HandoffProvider
    tools = CompositeToolProvider(
        existing_tools,
        HandoffProvider(multi_agent_manager, session_id_provider),
    )
"""
from __future__ import annotations

import hashlib
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class HandoffProvider(ToolProvider):
    """Auto-generate ``transfer_to_<agent>`` tools for every registered agent.

    Each handoff tool:
      - name: ``transfer_to_<agent_id>`` (e.g. ``transfer_to_code_reviewer``)
      - description: auto-generated from the target agent's persona
      - parameters: ``{ "task": string describing what to do }``
      - read_only: False (transfers state to another agent)
    """

    # Agents that should NOT get handoff tools (system/reserved agents).
    _EXCLUDED_AGENTS = frozenset({"main", "echo", "passthrough"})

    def __init__(
        self,
        multi_agent_manager: Any,  # xmclaw.daemon.multi_agent.MultiAgentManager
        session_id_provider: Any,  # callable → str
    ) -> None:
        self._mgr = multi_agent_manager
        self._sid_provider = session_id_provider
        self._tools: list[ToolSpec] = []
        self._agent_ids: list[str] = []
        self._refresh_tools()

    def _refresh_tools(self) -> None:
        tools: list[ToolSpec] = []
        agent_ids: list[str] = []
        try:
            for agent_id in self._mgr.list_agent_ids():
                if agent_id in self._EXCLUDED_AGENTS:
                    continue
                # Generate a stable 4-char hash suffix for the tool name
                # to avoid collisions when two agents have similar ids.
                hash_suffix = hashlib.sha1(
                    agent_id.encode("utf-8")
                ).hexdigest()[:4]
                name = f"transfer_to_{agent_id}"
                # Truncate to 64 chars for Anthropic compatibility
                if len(name) > 64:
                    name = f"transfer_to_{hash_suffix}"

                # Build description from agent persona if available
                desc = (
                    f"Transfer the task to agent '{agent_id}'. "
                    f"Use this when you need to delegate work that "
                    f"requires specialised knowledge or when you want "
                    f"to parallelise subtasks across agents. "
                    f"The target agent will receive the full task "
                    f"description you provide."
                )

                tools.append(ToolSpec(
                    name=name,
                    description=desc,
                    read_only=False,
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "A clear, detailed description of what "
                                    "the target agent should do. Include "
                                    "relevant context, file paths, and "
                                    "constraints."
                                ),
                            },
                        },
                        "required": ["task"],
                    },
                ))
                agent_ids.append(agent_id)
        except Exception:  # noqa: BLE001
            pass
        self._tools = tools
        self._agent_ids = agent_ids

    def list_tools(self) -> list[ToolSpec]:
        # Refresh on every list to catch newly registered agents
        self._refresh_tools()
        return list(self._tools)

    async def invoke(self, call: ToolCall) -> ToolResult:
        agent_id = call.name
        if agent_id.startswith("transfer_to_"):
            agent_id = agent_id[len("transfer_to_"):]
        # Try hash-suffix match if direct match fails
        if agent_id not in self._agent_ids:
            for aid in self._agent_ids:
                hs = hashlib.sha1(aid.encode("utf-8")).hexdigest()[:4]
                if agent_id == hs:
                    agent_id = aid
                    break
            else:
                return ToolResult(
                    call_id=call.id, ok=False, content=None,
                    error=f"Agent '{agent_id}' not found. Available: {', '.join(self._agent_ids)}",
                )

        task = (call.args or {}).get("task", "")
        if not task:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error="The 'task' parameter is required for agent handoff.",
            )

        try:
            agent = await self._mgr.get_or_start(agent_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"Failed to start agent '{agent_id}': {exc}",
            )

        try:
            sid = self._sid_provider() if callable(self._sid_provider) else "handoff"
            result = await agent.run_turn(
                session_id=f"{sid}_{agent_id}",
                user_message=task,
            )
            return ToolResult(
                call_id=call.id,
                ok=result.ok,
                content=result.text or result.error or "",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"Agent '{agent_id}' handoff failed: {exc}",
            )
