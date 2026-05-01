"""B-147 — /api/v2/channels router unit tests.

Pins:
  * GET surfaces every manifest (incl. scaffolds) + current config + running flag
  * GET redacts secret-shaped fields
  * PUT writes to ``config.channels.<id>``
  * PUT empty-string secret preserves the on-disk value
  * PUT redacted-form ('abcd…wxyz') is rejected as a noop on that field
  * PUT unknown channel id → 400
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.daemon.app import create_app


@pytest.fixture
def app_with_cfg(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    app = create_app(config={}, config_path=cfg_path)
    return app, cfg_path


def test_list_returns_known_manifests(app_with_cfg) -> None:
    app, _ = app_with_cfg
    with TestClient(app) as c:
        r = c.get("/api/v2/channels")
    assert r.status_code == 200
    data = r.json()
    ids = {ch["id"] for ch in data["channels"]}
    # discover(include_scaffolds=True) returns all 5 国内 IM
    assert {"feishu", "dingtalk", "wecom", "telegram"}.issubset(ids)


def test_list_carries_implementation_status(app_with_cfg) -> None:
    app, _ = app_with_cfg
    with TestClient(app) as c:
        r = c.get("/api/v2/channels")
    by_id = {ch["id"]: ch for ch in r.json()["channels"]}
    # B-145: feishu is now ready, others remain scaffold
    assert by_id["feishu"]["implementation_status"] == "ready"
    assert by_id["dingtalk"]["implementation_status"] == "scaffold"


def test_put_writes_channel_config(app_with_cfg) -> None:
    app, cfg_path = app_with_cfg
    with TestClient(app) as c:
        r = c.put("/api/v2/channels/feishu", json={
            "enabled": True,
            "app_id": "cli_test",
            "app_secret": "secret_real",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["restart_required"] is True
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["channels"]["feishu"]["app_id"] == "cli_test"
    assert on_disk["channels"]["feishu"]["app_secret"] == "secret_real"
    assert on_disk["channels"]["feishu"]["enabled"] is True


def test_put_empty_secret_preserves_existing(app_with_cfg) -> None:
    app, cfg_path = app_with_cfg
    cfg_path.write_text(json.dumps({
        "channels": {"feishu": {"app_id": "cli_x", "app_secret": "kept_secret"}},
    }), encoding="utf-8")
    # Recreate to pick up the seeded config
    app2 = create_app(
        config=json.loads(cfg_path.read_text(encoding="utf-8")),
        config_path=cfg_path,
    )
    with TestClient(app2) as c:
        r = c.put("/api/v2/channels/feishu", json={
            "enabled": True,
            "app_id": "cli_y",        # change
            "app_secret": "",         # empty → keep
        })
    assert r.status_code == 200
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["channels"]["feishu"]["app_id"] == "cli_y"
    assert on_disk["channels"]["feishu"]["app_secret"] == "kept_secret"


def test_put_redacted_form_does_not_overwrite(app_with_cfg) -> None:
    app, cfg_path = app_with_cfg
    cfg_path.write_text(json.dumps({
        "channels": {"feishu": {"app_secret": "the_real_secret"}},
    }), encoding="utf-8")
    app2 = create_app(
        config=json.loads(cfg_path.read_text(encoding="utf-8")),
        config_path=cfg_path,
    )
    with TestClient(app2) as c:
        r = c.put("/api/v2/channels/feishu", json={
            "app_secret": "the_…cret",  # mimic UI passing back the redacted form
        })
    assert r.status_code == 200
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["channels"]["feishu"]["app_secret"] == "the_real_secret"


def test_put_unknown_channel_rejected(app_with_cfg) -> None:
    app, _ = app_with_cfg
    with TestClient(app) as c:
        r = c.put("/api/v2/channels/no-such", json={"enabled": True})
    assert r.status_code == 400
    assert "unknown" in r.json()["error"].lower()


def test_get_redacts_secret_fields(app_with_cfg) -> None:
    """app_secret / encrypt_key / verify_token must come back masked."""
    app, cfg_path = app_with_cfg
    cfg_path.write_text(json.dumps({
        "channels": {"feishu": {
            "app_id": "cli_visible",
            "app_secret": "shouldnotleak123456",
        }},
    }), encoding="utf-8")
    app2 = create_app(
        config=json.loads(cfg_path.read_text(encoding="utf-8")),
        config_path=cfg_path,
    )
    with TestClient(app2) as c:
        r = c.get("/api/v2/channels")
    by_id = {ch["id"]: ch for ch in r.json()["channels"]}
    feishu_cfg = by_id["feishu"]["config"]
    # Non-secret stays visible
    assert feishu_cfg["app_id"] == "cli_visible"
    # Secret is masked (contains the … separator)
    assert "shouldnotleak" not in feishu_cfg["app_secret"]
    assert "…" in feishu_cfg["app_secret"]
