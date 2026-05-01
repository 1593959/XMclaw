"""Workspace: a bundle of state owned by a single agent (Epic #17 Phase 1).

Phase 1 scope is intentionally narrow: the dataclass and a factory that
wraps :func:`xmclaw.daemon.factory.build_agent_from_config`. These tests
lock in the contract that Phase 2's MultiAgentManager will build on —
agent_id injection, is_ready() semantics, to_dict() preset shape,
LLM-less graceful behavior, and max_hops propagation.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.workspace import Workspace, build_workspace


@pytest.fixture(autouse=True)
def _isolate_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin both secret stores under ``tmp_path`` to keep ``build_workspace``
    → ``build_llm_from_config`` → ``get_secret`` away from the developer's
    real keys.

    Without this, the "empty ``api_key`` returns not_ready" assertion is
    racy: if the host has ``llm.anthropic.api_key`` set in the encrypted
    store (Phase 2 default), the secrets-layer fallback resolves it and
    the workspace comes up ready, flipping the test green-then-red on
    machines with real credentials.
    """
    monkeypatch.setenv("XMC_SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("XMC_SECRET_DIR", str(tmp_path / ".xmclaw.secret"))
    for key in list(os.environ):
        if key.startswith("XMC_SECRET_") and key != "XMC_SECRET_DIR":
            monkeypatch.delenv(key, raising=False)


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
    # Still stamps agent_id + the default kind into config so persistence
    # round-trips cleanly. kind was added in Phase 7 — its presence in
    # the resolved config is how the rehydrate path knows which
    # workspace branch to take.
    assert ws.config == {"agent_id": "empty", "kind": "llm"}


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


# ── Phase 7: kind dispatch ──────────────────────────────────────────────


def test_default_kind_is_llm(bus: InProcessEventBus, llm_config: dict[str, object]) -> None:
    ws = build_workspace("k1", llm_config, bus)
    assert ws.kind == "llm"
    assert ws.observer is None


def test_evolution_kind_builds_observer_not_loop(bus: InProcessEventBus) -> None:
    ws = build_workspace("evo-1", {"kind": "evolution"}, bus)
    assert ws.kind == "evolution"
    assert ws.agent_loop is None
    assert ws.observer is not None
    assert ws.observer.agent_id == "evo-1"


def test_evolution_kind_is_ready_without_agent_loop(bus: InProcessEventBus) -> None:
    # LLM workspaces need a loop; evolution workspaces only need the
    # observer instance — start() installs the bus subscription.
    ws = build_workspace("evo-1", {"kind": "evolution"}, bus)
    assert ws.is_ready() is True


def test_unknown_kind_raises(bus: InProcessEventBus) -> None:
    with pytest.raises(ValueError, match="unknown workspace kind"):
        build_workspace("bad", {"kind": "nope"}, bus)


@pytest.mark.asyncio
async def test_evolution_start_installs_bus_subscription(
    bus: InProcessEventBus,
) -> None:
    # start() on an evolution workspace must make its observer live;
    # stop() must unwind it. This is the hook MultiAgentManager calls.
    ws = build_workspace("evo-1", {"kind": "evolution"}, bus)
    assert ws.observer is not None
    await ws.start()
    assert ws.observer.is_running() is True
    await ws.stop()
    assert ws.observer.is_running() is False


@pytest.mark.asyncio
async def test_llm_start_is_inert(
    bus: InProcessEventBus, llm_config: dict[str, object],
) -> None:
    # LLM workspaces' start/stop are no-ops; no raise, no background
    # work. The manager calls them uniformly for every kind.
    ws = build_workspace("llm-1", llm_config, bus)
    await ws.start()  # must not raise
    await ws.stop()   # must not raise
    assert ws.agent_loop is not None


# ── B-134: sub-agent inherits primary's llm when omitted ─────────────


def test_sub_agent_inherits_llm_from_primary(
    bus: InProcessEventBus, llm_config: dict[str, object],
) -> None:
    """Persona-template sub-agents ship only system_prompt; the LLM
    block falls through from the daemon's primary config."""
    sub_config = {"system_prompt": "你是测试子 agent"}
    ws = build_workspace(
        "sub", sub_config, bus, primary_config=llm_config,
    )
    assert ws.agent_loop is not None
    # The merged config that landed on the workspace contains the
    # inherited llm section.
    assert ws.config.get("llm") == llm_config["llm"]
    assert ws.config.get("system_prompt") == "你是测试子 agent"


def test_sub_agent_explicit_empty_llm_does_not_inherit(
    bus: InProcessEventBus, llm_config: dict[str, object],
) -> None:
    """An explicit empty {} signals 'no LLM, this agent is meant to be
    inert' — must override the primary inheritance."""
    sub_config = {"llm": {}, "system_prompt": "i'm intentionally inert"}
    ws = build_workspace(
        "sub", sub_config, bus, primary_config=llm_config,
    )
    assert ws.config.get("llm") == {}


def test_no_primary_config_no_inheritance(bus: InProcessEventBus) -> None:
    """Without primary_config the sub-agent's llm absence is
    preserved — no llm in, no llm out."""
    ws = build_workspace("sub", {"system_prompt": "x"}, bus)
    assert "llm" not in ws.config or ws.config.get("llm") is None
