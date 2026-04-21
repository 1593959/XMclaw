"""create_app(config=...) — auto-builds the agent from config.

This is the "xmclaw v2 serve out of the box" wiring: no hand-
constructed AgentLoop, just a config dict (or a real daemon/config.json
file) and the daemon comes up with a live agent.
"""
from __future__ import annotations

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.app_v2 import create_app


def test_create_app_with_config_wires_agent_automatically() -> None:
    bus = InProcessEventBus()
    app = create_app(
        bus=bus,
        config={
            "llm": {"anthropic": {
                "api_key": "sk-placeholder", "default_model": "claude-haiku-4-5",
            }},
        },
    )
    # app.state.agent should now be a live AgentLoop.
    assert app.state.agent is not None
    assert isinstance(app.state.agent, AgentLoop)


def test_create_app_with_empty_config_stays_in_echo_mode() -> None:
    app = create_app(config={"llm": {}})
    assert app.state.agent is None


def test_explicit_agent_wins_over_config() -> None:
    """If the caller passes both, the explicit agent takes precedence."""
    bus = InProcessEventBus()
    from xmclaw.providers.llm.anthropic import AnthropicLLM
    explicit_agent = AgentLoop(
        llm=AnthropicLLM(api_key="test"),
        bus=bus,
    )
    app = create_app(
        bus=bus,
        agent=explicit_agent,
        config={  # would build a DIFFERENT agent — must be ignored
            "llm": {"anthropic": {"api_key": "other-key"}},
        },
    )
    assert app.state.agent is explicit_agent


def test_create_app_without_anything_is_echo_mode() -> None:
    app = create_app()
    assert app.state.agent is None
