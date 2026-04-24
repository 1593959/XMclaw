"""End-to-end Phase 5 wiring — the primary agent's ``_tools`` composite
must include the 4 agent-to-agent tools alongside whatever the factory
built from config.

Unit tests in ``tests/unit/test_v2_agent_inter_tools.py`` already cover
tool-level logic; this file only locks the ``create_app`` assembly step
so a future refactor that forgets to wire ``AgentInterTools`` shows up
as a red here rather than as a silent capability loss.
"""
from __future__ import annotations

from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app


_LLM_CONFIG: dict[str, Any] = {
    "llm": {
        "anthropic": {
            "api_key": "sk-ant-test",
            "default_model": "claude-haiku-4-5",
        },
    },
}


def test_primary_agent_gets_inter_agent_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    bus = InProcessEventBus()
    app = create_app(bus=bus, config=_LLM_CONFIG)
    agent = app.state.agent
    assert agent is not None
    assert agent._tools is not None
    names = {s.name for s in agent._tools.list_tools()}
    # The four must be present alongside the config-wired builtins.
    assert {
        "list_agents", "chat_with_agent",
        "submit_to_agent", "check_agent_task",
    }.issubset(names)
    # And the builtins are still there — the composite unioned them,
    # didn't replace them.
    assert {"file_read", "bash"}.issubset(names)


@pytest.mark.asyncio
async def test_list_agents_tool_sees_registered_worker(
    tmp_path, monkeypatch,
) -> None:
    # Post a worker agent via the manager surface, then invoke
    # ``list_agents`` via the tool: the worker should appear.
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path))
    bus = InProcessEventBus()
    app = create_app(bus=bus, config=_LLM_CONFIG)
    agent = app.state.agent
    manager = app.state.agents
    await manager.create("helper", _LLM_CONFIG)

    from xmclaw.core.ir import ToolCall
    call = ToolCall(name="list_agents", args={}, provenance="synthetic")
    result = await agent._tools.invoke(call)
    assert result.ok
    import json as _json
    body = _json.loads(result.content)
    ids = [row["agent_id"] for row in body["agents"]]
    assert "main" in ids and "helper" in ids
