from __future__ import annotations

from xmclaw.daemon.typed_config import parse_typed_config, typed_config_errors


def test_typed_config_parses_high_risk_defaults() -> None:
    cfg = parse_typed_config({})

    assert cfg.tools.enable_bash is True
    assert cfg.tools.shell.execution_policy == "host_guarded"
    assert cfg.tools.shell.resolved_image == "python:3.12-alpine"
    assert cfg.agent.resolved_max_hops == 100
    assert cfg.cognition.self_critique.enabled is True


def test_typed_config_accepts_shell_and_agent_overrides() -> None:
    cfg = parse_typed_config({
        "tools": {
            "enable_bash": False,
            "invoke_timeout_s": 240.0,
            "shell": {
                "execution_policy": "docker",
                "sandbox_image": "alpine:3.20",
                "sandbox_memory": "1g",
                "sandbox_cpus": "0.75",
                "sandbox_pids_limit": 64,
                "sandbox_network": "bridge",
            },
        },
        "agent": {"max_react_loop": 12},
        "swarm": {
            "enabled": True,
            "max_subagents": 8,
            "max_depth": 3,
            "task_timeout_s": 120.0,
            "synthesize": False,
        },
        "evolution": {
            "skill_dream": {"enabled": True, "interval_s": 1800.0},
            "realtime": {
                "enabled": True,
                "debounce_s": 15.0,
                "cooldown_s": 60.0,
            },
        },
        "cognition": {
            "auto_recall": {
                "enabled": True,
                "use_hybrid": True,
                "timeout_s": 2.5,
                "min_similarity": 0.7,
            },
            "continuous_loop": {
                "autonomy_level": 75,
                "heartbeat_hz": 0.5,
            },
            "self_critique": {"enabled": False},
            "skill_proposer": {
                "history_window": 50,
                "min_pattern_count": 3,
                "min_confidence": 0.6,
            },
        },
    })

    assert cfg.tools.enable_bash is False
    assert cfg.tools.invoke_timeout_s == 240.0
    assert cfg.tools.shell.execution_policy == "docker"
    assert cfg.tools.shell.resolved_image == "alpine:3.20"
    assert cfg.tools.shell.resolved_memory == "1g"
    assert cfg.tools.shell.resolved_cpus == "0.75"
    assert cfg.tools.shell.resolved_pids_limit == 64
    assert cfg.tools.shell.resolved_network == "bridge"
    assert cfg.agent.resolved_max_hops == 12
    assert cfg.swarm.enabled is True
    assert cfg.swarm.max_subagents == 8
    assert cfg.swarm.max_depth == 3
    assert cfg.swarm.task_timeout_s == 120.0
    assert cfg.swarm.synthesize is False
    assert cfg.evolution.skill_dream.enabled is True
    assert cfg.evolution.skill_dream.interval_s == 1800.0
    assert cfg.evolution.realtime.debounce_s == 15.0
    assert cfg.evolution.realtime.cooldown_s == 60.0
    assert cfg.cognition.auto_recall.enabled is True
    assert cfg.cognition.auto_recall.use_hybrid is True
    assert cfg.cognition.auto_recall.timeout_s == 2.5
    assert cfg.cognition.auto_recall.min_similarity == 0.7
    assert cfg.cognition.continuous_loop.autonomy_level == 75
    assert cfg.cognition.continuous_loop.heartbeat_hz == 0.5
    assert cfg.cognition.self_critique.enabled is False
    assert cfg.cognition.skill_proposer.history_window == 50
    assert cfg.cognition.skill_proposer.min_pattern_count == 3
    assert cfg.cognition.skill_proposer.min_confidence == 0.6


def test_typed_config_shell_compat_aliases_take_effect() -> None:
    cfg = parse_typed_config({
        "tools": {
            "shell_execution_policy": "disabled",
            "shell": {
                "image": "ubuntu:24.04",
                "memory": "2g",
                "cpus": "2.0",
                "pids_limit": 512,
                "network": "bridge",
            },
        },
    })

    assert cfg.tools.resolved_shell_policy == "disabled"
    assert cfg.tools.shell.resolved_image == "ubuntu:24.04"
    assert cfg.tools.shell.resolved_memory == "2g"
    assert cfg.tools.shell.resolved_cpus == "2.0"
    assert cfg.tools.shell.resolved_pids_limit == 512
    assert cfg.tools.shell.resolved_network == "bridge"


def test_typed_config_reports_invalid_values() -> None:
    errors = typed_config_errors({
        "tools": {
            "enable_bash": "yes",
            "invoke_timeout_s": 0,
            "shell": {
                "execution_policy": "mystery",
                "sandbox_image": "",
                "sandbox_pids_limit": 1,
                "sandbox_network": "host",
            },
        },
        "agent": {"max_react_loop": 0},
        "swarm": {
            "enabled": "yes",
            "max_subagents": 1,
            "max_depth": 0,
            "task_timeout_s": 0,
            "synthesize": "no",
        },
        "evolution": {
            "skill_dream": {"enabled": "yes", "interval_s": 0},
            "realtime": {
                "enabled": "yes",
                "debounce_s": 0,
                "cooldown_s": -1,
            },
        },
        "cognition": {
            "auto_recall": {
                "enabled": "yes",
                "use_hybrid": "yes",
                "timeout_s": 0,
                "min_similarity": 1.5,
            },
            "continuous_loop": {
                "autonomy_level": 101,
                "heartbeat_hz": 0,
            },
            "self_critique": {"enabled": "yes"},
            "skill_proposer": {
                "history_window": 0,
                "min_pattern_count": 0,
                "min_confidence": 1.5,
            },
        },
    })

    joined = "\n".join(errors)
    assert "tools.enable_bash" in joined
    assert "tools.invoke_timeout_s" in joined
    assert "tools.shell.execution_policy" in joined
    assert "tools.shell.sandbox_image" in joined
    assert "tools.shell.sandbox_pids_limit" in joined
    assert "tools.shell.sandbox_network" in joined
    assert "agent.max_react_loop" in joined
    assert "swarm.enabled" in joined
    assert "swarm.max_subagents" in joined
    assert "swarm.max_depth" in joined
    assert "swarm.task_timeout_s" in joined
    assert "swarm.synthesize" in joined
    assert "evolution.skill_dream.enabled" in joined
    assert "evolution.skill_dream.interval_s" in joined
    assert "evolution.realtime.enabled" in joined
    assert "evolution.realtime.debounce_s" in joined
    assert "evolution.realtime.cooldown_s" in joined
    assert "cognition.auto_recall.enabled" in joined
    assert "cognition.auto_recall.use_hybrid" in joined
    assert "cognition.auto_recall.timeout_s" in joined
    assert "cognition.auto_recall.min_similarity" in joined
    assert "cognition.continuous_loop.autonomy_level" in joined
    assert "cognition.continuous_loop.heartbeat_hz" in joined
    assert "cognition.self_critique.enabled" in joined
    assert "cognition.skill_proposer.history_window" in joined
    assert "cognition.skill_proposer.min_pattern_count" in joined
    assert "cognition.skill_proposer.min_confidence" in joined


def test_typed_config_validates_memory_v2_blocks() -> None:
    cfg = parse_typed_config({
        "cognition": {
            "memory_v2": {
                "retention": {
                    "sweep_interval_s": 60.0,
                    "dedup_every_n_sweeps": 3,
                    "llm_dedup_every_n_sweeps": 5,
                    "dedup_scopes": ["user", "project"],
                },
                "curator": {
                    "enabled": True,
                    "interval_s": 300.0,
                    "time_budget_s": 5,
                    "scopes": ["user"],
                },
                "write_decision": {"enabled": False},
            },
        },
    })

    memory = cfg.cognition.memory_v2
    assert memory.retention.dedup_every_n_sweeps == 3
    assert memory.curator.enabled is True
    assert memory.curator.time_budget_s == 5
    assert memory.write_decision.enabled is False


def test_typed_config_reports_invalid_memory_v2_blocks() -> None:
    errors = typed_config_errors({
        "cognition": {
            "memory_v2": {
                "retention": {
                    "sweep_interval_s": -1.0,
                    "dedup_scopes": ["user", 1],
                },
                "curator": {
                    "enabled": "yes",
                    "interval_s": 0,
                    "scopes": ["user", 1],
                },
                "write_decision": {"enabled": "yes"},
            },
        },
    })

    joined = "\n".join(errors)
    assert "cognition.memory_v2.retention.sweep_interval_s" in joined
    assert "cognition.memory_v2.retention.dedup_scopes.1" in joined
    assert "cognition.memory_v2.curator.enabled" in joined
    assert "cognition.memory_v2.curator.interval_s" in joined
    assert "cognition.memory_v2.curator.scopes.1" in joined
    assert "cognition.memory_v2.write_decision.enabled" in joined
