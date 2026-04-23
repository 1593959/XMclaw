"""Workspace: a bundle of state owned by a single agent (Epic #17 Phase 1).

Phase 1 scope is intentionally narrow: the dataclass and a factory that
wraps :func:`xmclaw.daemon.factory.build_agent_from_config`. These tests
lock in the contract that Phase 2's MultiAgentManager will build on —
agent_id injection, is_ready() semantics, to_dict() preset shape,
LLM-less graceful behavior, and max_hops propagation.
"""
from __future__ import annotations

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.workspace import Workspace, build_workspace


@pytest.fixture
def bus() -> InProcessEventBus:
    return InProcessEventBus()


@pytest.fixture
def llm_config() -> dict[str, object]:
    # Minimal real-looking config: Anthropic w/ placeholder key. factory
    # won't call the API — it just instantiates AnthropicLLM.
    return {
        "llm": {
            "anthropic": {
                "api_key": "sk-ant-test",
                "default_model": "claude-haiku-4-5",
            },
        },
    }


# ── Workspace dataclass ──────────────────────────────────────────────────


def test_workspace_defaults_empty_config_and_no_loop() -> None:
    ws = Workspace(agent_id="a1")
    assert ws.agent_id == "a1"
    assert ws.config == {}
    assert ws.agent_loop is None


def test_is_ready_false_without_loop() -> None:
    assert Workspace(agent_id="a1").is_ready() is False


def test_is_ready_true_with_loop(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    ws = build_workspace("a1", llm_config, bus)
    assert ws.is_ready() is True
    assert isinstance(ws.agent_loop, AgentLoop)


def test_to_dict_drops_agent_loop_and_preserves_agent_id() -> None:
    ws = Workspace(agent_id="a1", config={"llm": {"openai": {}}}, agent_loop=None)
    d = ws.to_dict()
    assert d["agent_id"] == "a1"
    assert d["llm"] == {"openai": {}}
    assert "agent_loop" not in d


def test_to_dict_preset_shape_is_json_shaped(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    # Phase 2 will serialize this to ~/.xmclaw/workspaces/<id>.json —
    # the shape must be JSON-serializable, not contain the live loop.
    ws = build_workspace("persist-me", llm_config, bus)
    d = ws.to_dict()
    import json
    # Round-trips through json without error.
    roundtripped = json.loads(json.dumps(d))
    assert roundtripped["agent_id"] == "persist-me"
    assert roundtripped["llm"]["anthropic"]["default_model"] == "claude-haiku-4-5"


def test_workspace_is_mutable_not_frozen() -> None:
    # Phase 2 needs to attach per-workspace resources post-hoc.
    ws = Workspace(agent_id="a1")
    ws.config = {"changed": True}
    assert ws.config == {"changed": True}


# ── build_workspace factory ──────────────────────────────────────────────


def test_build_workspace_injects_agent_id_into_config(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    ws = build_workspace("my-agent-id", llm_config, bus)
    assert ws.config["agent_id"] == "my-agent-id"
    assert ws.agent_id == "my-agent-id"


def test_build_workspace_agent_id_reaches_agent_loop(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    # The AgentLoop stamps agent_id onto every event it emits, so the
    # inject-then-build path is the critical one to guard.
    ws = build_workspace("router-1", llm_config, bus)
    assert ws.agent_loop is not None
    assert ws.agent_loop._agent_id == "router-1"


def test_build_workspace_caller_agent_id_wins_over_config(bus: InProcessEventBus) -> None:
    # If the preset dict happens to contain an agent_id already, the
    # caller's explicit arg must override it — the manager owns the ID.
    cfg = {
        "agent_id": "stale-id",
        "llm": {"anthropic": {"api_key": "sk-ant-test", "default_model": "claude-haiku-4-5"}},
    }
    ws = build_workspace("fresh-id", cfg, bus)
    assert ws.agent_id == "fresh-id"
    assert ws.config["agent_id"] == "fresh-id"
    assert ws.agent_loop is not None
    assert ws.agent_loop._agent_id == "fresh-id"


def test_build_workspace_returns_not_ready_when_no_llm(bus: InProcessEventBus) -> None:
    # Empty preset shape (user hasn't picked a provider yet) is valid —
    # the caller decides how to surface "not ready" to the client.
    ws = build_workspace("empty", {}, bus)
    assert ws.agent_id == "empty"
    assert ws.agent_loop is None
    assert ws.is_ready() is False
    # Still stamps agent_id into config so persistence round-trips cleanly.
    assert ws.config == {"agent_id": "empty"}


def test_build_workspace_returns_not_ready_when_llm_key_missing(bus: InProcessEventBus) -> None:
    ws = build_workspace(
        "no-key",
        {"llm": {"anthropic": {"api_key": "", "default_model": "claude-haiku-4-5"}}},
        bus,
    )
    assert ws.is_ready() is False


def test_build_workspace_propagates_max_hops(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    ws = build_workspace("hoppy", llm_config, bus, max_hops=7)
    assert ws.agent_loop is not None
    assert ws.agent_loop._max_hops == 7


def test_build_workspace_does_not_mutate_input_config(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    # Phase 2's manager will re-use the same preset dict across multiple
    # build_workspace() calls if the user clones a preset — leaking
    # agent_id back into the caller's dict would corrupt that flow.
    before = dict(llm_config)
    build_workspace("stamp-1", llm_config, bus)
    assert llm_config == before
    assert "agent_id" not in llm_config
