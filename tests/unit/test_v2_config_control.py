from __future__ import annotations

import json

from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


def test_config_control_surfaces_grouped_safe_fields(tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg = {
        "security": {
            "prompt_injection": "detect_only",
            "guardians": {"enabled": False, "computer_use_mode": "allow"},
        },
        "tools": {
            "enable_bash": True,
            "enable_web": True,
            "shell": {"execution_policy": "host_guarded"},
        },
        "voice": {
            "stt": {"model": "tiny", "device": "cpu"},
            "tts": {"voice": "zh-CN-XiaoxiaoNeural"},
        },
        "evolution": {
            "memory": {
                "embedding": {
                    "provider": "openai",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "qwen3-embedding:0.6b",
                    "dimensions": 1024,
                },
            },
        },
        "cognition": {
            "memory_v2": {
                "enabled": True,
                "recall_top_k": 5,
                "candidates": {"min_quality_score": 0.45},
                "md_sync": {"enabled": True, "direction": "manual_to_facts"},
            },
            "continuous_loop": {"enabled": True, "autonomy_level": 50},
        },
        "skills": {
            "disclosure_mode": "auto",
            "semantic_discovery": {"enabled": True, "floor": 0.3},
            "autonomous_invocation": {
                "enabled": True,
                "mode": "prefer",
                "max_loaded": 2,
                "require_decision": True,
            },
            "install": {"compatibility_mode": "adapt_markdown"},
        },
        "agent": {"max_hops": 100, "state_graph": {"enabled": True}},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    app = create_app(config=cfg, config_path=cfg_path)

    r = TestClient(app).get("/api/v2/config/control")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert {"security", "voice", "models", "memory", "skills", "runtime"} <= set(data["groups"])
    prompt_policy = next(f for f in data["fields"] if f["path"] == "security.prompt_injection")
    assert prompt_policy["value"] == "detect_only"
    assert prompt_policy["label"] == "提示词注入策略"
    skill_mode = next(f for f in data["fields"] if f["path"] == "skills.autonomous_invocation.mode")
    assert skill_mode["value"] == "prefer"
    assert skill_mode["label"] == "自主技能策略"
    memory_quality = next(
        f for f in data["fields"]
        if f["path"] == "cognition.memory_v2.candidates.min_quality_score"
    )
    assert memory_quality["value"] == 0.45
    assert memory_quality["label"] == "候选记忆最低质量分"
    md_sync = next(f for f in data["fields"] if f["path"] == "cognition.memory_v2.md_sync.direction")
    assert md_sync["value"] == "manual_to_facts"
    assert md_sync["label"] == "MD 同步方向"
    state_graph = next(f for f in data["fields"] if f["path"] == "agent.state_graph.enabled")
    assert state_graph["value"] is True
    assert state_graph["label"] == "启用 StateGraph 回合状态"


def test_config_control_patch_writes_allowed_paths_and_flags_restart(tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg = {
        "security": {"prompt_injection": "detect_only"},
        "tools": {"enable_bash": True},
        "agent": {"max_hops": 100},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    app = create_app(config=cfg, config_path=cfg_path)

    r = TestClient(app).patch(
        "/api/v2/config/control",
        json={"patch": {"security.prompt_injection": "block", "agent.max_hops": 24}},
    )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["restart_required"] is True
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["security"]["prompt_injection"] == "block"
    assert saved["agent"]["max_hops"] == 24
    assert app.state.config["agent"]["max_hops"] == 24
    assert list(tmp_path.glob("config.json.bak-*"))


def test_config_control_rejects_unknown_or_bad_paths(tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg = {"security": {"prompt_injection": "detect_only"}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    app = create_app(config=cfg, config_path=cfg_path)
    client = TestClient(app)

    unknown = client.patch(
        "/api/v2/config/control",
        json={"patch": {"llm.openai.api_key": "sk-leak"}},
    )
    bad_type = client.post(
        "/api/v2/config/control/validate",
        json={"patch": {"cognition.continuous_loop.autonomy_level": 500}},
    )

    assert unknown.status_code == 400
    assert "unsupported config path" in unknown.json()["error"]
    assert bad_type.status_code == 400
    assert bad_type.json()["ok"] is False
