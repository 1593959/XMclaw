"""Phase 10 — Proma 式渠道开关：profile ``enabled`` 字段。

两层：
  1. factory.build_llm_profiles_from_config 对 ``enabled:false`` 跳过加载
     （保留配置 + api_key，仅不进 registry）；missing/true = 加载（back-compat）。
  2. PATCH /api/v2/llm/profiles/{id}/enabled 端到端（TestClient 真 URL）：
     翻转配置标志，只动 enabled 不丢其他字段。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.factory import build_llm_profiles_from_config


def _cfg(enabled_flag) -> dict:
    entry = {
        "id": "p-test",
        "label": "Test",
        "provider": "openai_compat",
        "model": "test-model",
        "api_key": "sk-xxx",
        "base_url": "https://example.com/v1",
        "max_tokens": 4096,
    }
    if enabled_flag is not None:
        entry["enabled"] = enabled_flag
    return {"llm": {"profiles": [entry]}}


# ── Layer 1: factory skip ──────────────────────────────────────────


def test_enabled_false_skips_registry_load() -> None:
    out = build_llm_profiles_from_config(_cfg(False))
    assert all(p.id != "p-test" for p in out), "enabled:false profile 不应进 registry"


def test_enabled_true_loads() -> None:
    out = build_llm_profiles_from_config(_cfg(True))
    assert any(p.id == "p-test" for p in out)


def test_missing_enabled_loads_backcompat() -> None:
    """既有配置无 enabled 字段 → 照常加载（向后兼容）。"""
    out = build_llm_profiles_from_config(_cfg(None))
    assert any(p.id == "p-test" for p in out)


# ── Layer 2: PATCH 端到端 ──────────────────────────────────────────


def test_patch_disable_persists_flag(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_cfg(None)), encoding="utf-8")
    app = create_app(bus=InProcessEventBus(), config={})
    app.state.config_path = str(cfg_path)
    client = TestClient(app)

    r = client.patch("/api/v2/llm/profiles/p-test/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    entry = cfg["llm"]["profiles"][0]
    assert entry["enabled"] is False
    # 只动 enabled — 其他字段无损。
    assert entry["max_tokens"] == 4096
    assert entry["api_key"] == "sk-xxx"


def test_patch_enable_removes_flag(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_cfg(False)), encoding="utf-8")
    app = create_app(bus=InProcessEventBus(), config={})
    app.state.config_path = str(cfg_path)
    client = TestClient(app)

    r = client.patch("/api/v2/llm/profiles/p-test/enabled", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    entry = json.loads(cfg_path.read_text(encoding="utf-8"))["llm"]["profiles"][0]
    assert "enabled" not in entry  # missing = enabled


def test_patch_unknown_profile_404(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_cfg(None)), encoding="utf-8")
    app = create_app(bus=InProcessEventBus(), config={})
    app.state.config_path = str(cfg_path)
    client = TestClient(app)

    r = client.patch("/api/v2/llm/profiles/nope/enabled", json={"enabled": False})
    assert r.status_code == 404


def test_get_profiles_reports_enabled() -> None:
    # GET on_disk 读 app.state.config（内存 dict），不是文件。
    app = create_app(bus=InProcessEventBus(), config=_cfg(False))
    client = TestClient(app)

    r = client.get("/api/v2/llm/profiles")
    assert r.status_code == 200
    on_disk = r.json()["on_disk"]
    entry = next((e for e in on_disk if e["id"] == "p-test"), None)
    assert entry is not None
    assert entry["enabled"] is False
