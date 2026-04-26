"""Multi-model: LLMRegistry construction + AgentLoop per-turn switch.

Covers the chain that lets the user deploy several LLM endpoints and
pick which one to use per chat session:

* :func:`xmclaw.daemon.factory.build_llm_registry_from_config` — both
  legacy ``llm.{anthropic,openai}`` blocks and the new ``llm.profiles``
  array end up addressable in one registry.
* :meth:`xmclaw.daemon.agent_loop.AgentLoop.run_turn` — when called
  with ``llm_profile_id``, the matching profile's LLM handles the turn;
  unknown ids fall back to the default so a stale UI selection doesn't
  500 the user.
* ``GET /api/v2/llm/profiles`` — list redacts api_key, returns the
  default id, and reflects the registry attached to ``app.state``.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.app import create_app
from xmclaw.daemon.factory import (
    build_llm_profiles_from_config,
    build_llm_registry_from_config,
)
from xmclaw.daemon.llm_registry import LLMProfile, LLMRegistry
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Message


# ── A minimal fake LLM that records every complete() call ────────────


class _RecordingLLM(LLMProvider):
    """Just enough of LLMProvider to satisfy AgentLoop.run_turn."""

    def __init__(self, *, model: str, reply: str = "ok") -> None:
        self.model = model
        self.reply = reply
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], *, tools: Any = None) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(
            content=self.reply, tool_calls=[],
            prompt_tokens=0, completion_tokens=0,
        )

    async def stream(self, messages: list[Message], *, tools: Any = None):  # type: ignore[override]
        raise NotImplementedError

    @property
    def tool_call_shape(self) -> str:
        return "openai"

    @property
    def pricing(self):  # type: ignore[override]
        return None


# ── Factory tests ────────────────────────────────────────────────────


class TestBuildRegistry:
    def test_empty_config_yields_empty_registry(self) -> None:
        r = build_llm_registry_from_config({})
        assert len(r) == 0
        assert r.default_id is None
        assert r.default() is None

    def test_legacy_anthropic_block_becomes_default_profile(self) -> None:
        cfg = {"llm": {"anthropic": {
            "api_key": "sk-test",
            "default_model": "claude-haiku-4-5-20251001",
        }}}
        r = build_llm_registry_from_config(cfg)
        assert r.default_id == "default"
        assert "default" in r
        prof = r.default()
        assert prof is not None
        assert prof.model == "claude-haiku-4-5-20251001"

    def test_named_profiles_array_is_registered(self) -> None:
        cfg = {"llm": {"profiles": [
            {"id": "fast", "label": "Fast Haiku", "provider": "anthropic",
             "model": "claude-haiku-4-5-20251001", "api_key": "sk-1"},
            {"id": "smart", "provider": "openai",
             "model": "gpt-4o", "api_key": "sk-2"},
        ]}}
        r = build_llm_registry_from_config(cfg)
        assert r.ids() == ["fast", "smart"]
        assert r.default_id == "fast"  # first online when no legacy block
        assert r.get("smart").label == "smart"  # label falls back to id

    def test_legacy_plus_named_profiles_coexist(self) -> None:
        cfg = {"llm": {
            "anthropic": {"api_key": "sk-default", "default_model": "claude-haiku-4-5-20251001"},
            "profiles": [{"id": "extra", "provider": "openai",
                          "model": "gpt-4o-mini", "api_key": "sk-x"}],
        }}
        r = build_llm_registry_from_config(cfg)
        assert r.ids() == ["default", "extra"]
        assert r.default_id == "default"  # legacy wins

    def test_profile_without_api_key_is_dropped(self) -> None:
        cfg = {"llm": {"profiles": [
            {"id": "broken", "provider": "anthropic", "model": "x", "api_key": ""},
            {"id": "ok", "provider": "openai", "model": "gpt-4o", "api_key": "sk-1"},
        ]}}
        r = build_llm_registry_from_config(cfg)
        assert r.ids() == ["ok"]

    def test_unknown_provider_is_dropped(self) -> None:
        cfg = {"llm": {"profiles": [
            {"id": "weird", "provider": "ollama", "model": "llama", "api_key": "x"},
            {"id": "ok", "provider": "anthropic", "model": "claude", "api_key": "k"},
        ]}}
        assert build_llm_registry_from_config(cfg).ids() == ["ok"]

    def test_duplicate_ids_keep_first(self) -> None:
        cfg = {"llm": {"profiles": [
            {"id": "a", "provider": "anthropic", "model": "m1", "api_key": "k1"},
            {"id": "a", "provider": "openai", "model": "m2", "api_key": "k2"},
        ]}}
        prof = build_llm_profiles_from_config(cfg)
        assert [p.id for p in prof] == ["a"]
        assert prof[0].provider_name == "anthropic"


# ── LLMRegistry invariants ───────────────────────────────────────────


class TestRegistry:
    def test_default_id_must_exist(self) -> None:
        with pytest.raises(ValueError):
            LLMRegistry(profiles={}, default_id="nonexistent")

    def test_iteration_and_membership(self) -> None:
        a = LLMProfile(id="a", label="A", provider_name="anthropic",
                       model="m", llm=_RecordingLLM(model="m"))
        b = LLMProfile(id="b", label="B", provider_name="openai",
                       model="m2", llm=_RecordingLLM(model="m2"))
        r = LLMRegistry(profiles={"a": a, "b": b}, default_id="a")
        assert "a" in r
        assert "missing" not in r
        assert {p.id for p in r} == {"a", "b"}
        assert r.default().id == "a"


# ── AgentLoop per-turn LLM switch ────────────────────────────────────


class TestAgentLoopProfileSwitch:
    @pytest.mark.asyncio
    async def test_unset_profile_uses_default_llm(self) -> None:
        default_llm = _RecordingLLM(model="default-m", reply="from-default")
        other = _RecordingLLM(model="other-m", reply="from-other")
        registry = LLMRegistry(
            profiles={
                "default": LLMProfile(
                    id="default", label="d", provider_name="anthropic",
                    model="default-m", llm=default_llm,
                ),
                "other": LLMProfile(
                    id="other", label="o", provider_name="openai",
                    model="other-m", llm=other,
                ),
            },
            default_id="default",
        )
        loop = AgentLoop(
            llm=default_llm, bus=InProcessEventBus(),
            llm_registry=registry, max_hops=1,
        )
        result = await loop.run_turn("s1", "hi")
        assert result.text == "from-default"
        assert len(default_llm.calls) == 1
        assert len(other.calls) == 0

    @pytest.mark.asyncio
    async def test_known_profile_id_routes_to_that_llm(self) -> None:
        default_llm = _RecordingLLM(model="default-m")
        other = _RecordingLLM(model="other-m", reply="from-other")
        registry = LLMRegistry(
            profiles={
                "default": LLMProfile(
                    id="default", label="d", provider_name="anthropic",
                    model="default-m", llm=default_llm,
                ),
                "other": LLMProfile(
                    id="other", label="o", provider_name="openai",
                    model="other-m", llm=other,
                ),
            },
            default_id="default",
        )
        loop = AgentLoop(
            llm=default_llm, bus=InProcessEventBus(),
            llm_registry=registry, max_hops=1,
        )
        result = await loop.run_turn("s2", "hi", llm_profile_id="other")
        assert result.text == "from-other"
        assert len(default_llm.calls) == 0
        assert len(other.calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_profile_id_falls_back_to_default(self) -> None:
        # Stale UI state shouldn't 500 the user. Quietly use the default.
        default_llm = _RecordingLLM(model="default-m", reply="fallback")
        registry = LLMRegistry(
            profiles={"default": LLMProfile(
                id="default", label="d", provider_name="anthropic",
                model="default-m", llm=default_llm,
            )},
            default_id="default",
        )
        loop = AgentLoop(
            llm=default_llm, bus=InProcessEventBus(),
            llm_registry=registry, max_hops=1,
        )
        result = await loop.run_turn("s3", "hi", llm_profile_id="never-existed")
        assert result.text == "fallback"
        assert len(default_llm.calls) == 1


# ── HTTP router tests ────────────────────────────────────────────────


@pytest.fixture
def app_with_registry(tmp_path):
    """An app whose state.llm_registry has two profiles."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    cfg = {"llm": {"profiles": [
        {"id": "a", "label": "Aye", "provider": "anthropic",
         "model": "claude-haiku-4-5-20251001", "api_key": "sk-aaaaaaaa1111"},
        {"id": "b", "label": "Bee", "provider": "openai",
         "model": "gpt-4o-mini", "api_key": "sk-bbbbbbbb2222", "base_url": "http://x"},
    ]}}
    app = create_app(config=cfg, config_path=cfg_path)
    # No real agent built (no LLM keys in env), so wire a registry by hand
    # to exercise the router without needing a live SDK client.
    app.state.llm_registry = build_llm_registry_from_config(cfg)
    app.state.config = cfg
    return app


class TestProfilesRouter:
    def test_get_lists_runtime_and_on_disk(self, app_with_registry):
        with TestClient(app_with_registry) as c:
            resp = c.get("/api/v2/llm/profiles")
        assert resp.status_code == 200
        body = resp.json()
        assert body["default_id"] == "a"
        ids = [p["id"] for p in body["profiles"]]
        assert ids == ["a", "b"]
        assert body["profiles"][0]["label"] == "Aye"
        # api_key never appears in plaintext.
        for entry in body["on_disk"]:
            assert entry["api_key_redacted"]
            assert "sk-aaaaaaaa1111" not in entry["api_key_redacted"]
            assert "sk-bbbbbbbb2222" not in entry["api_key_redacted"]

    def test_post_writes_to_config_json(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.post("/api/v2/llm/profiles", json={
                "id": "new-one", "label": "New", "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001", "api_key": "sk-deadbeef",
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["restart_required"] is True
        # The file now has the entry.
        import json
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["llm"]["profiles"][0]["id"] == "new-one"
        assert on_disk["llm"]["profiles"][0]["api_key"] == "sk-deadbeef"

    def test_post_rejects_reserved_id(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.post("/api/v2/llm/profiles", json={
                "id": "default", "provider": "anthropic",
                "model": "x", "api_key": "k",
            })
        assert resp.status_code == 400
        assert "default" in resp.json()["error"]

    def test_post_rejects_bad_id_chars(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.post("/api/v2/llm/profiles", json={
                "id": "Bad ID!", "provider": "anthropic",
                "model": "x", "api_key": "k",
            })
        assert resp.status_code == 400

    def test_post_preserves_existing_api_key_when_blank(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            '{"llm": {"profiles": [{"id":"keep","provider":"anthropic","model":"m1","api_key":"sk-secret"}]}}',
            encoding="utf-8",
        )
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.post("/api/v2/llm/profiles", json={
                "id": "keep", "provider": "anthropic",
                "model": "m2", "api_key": "",   # empty → keep existing
            })
        assert resp.status_code == 200
        import json
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["llm"]["profiles"][0]["api_key"] == "sk-secret"
        assert on_disk["llm"]["profiles"][0]["model"] == "m2"

    def test_delete_removes_profile_from_disk(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            '{"llm": {"profiles": [{"id":"gone","provider":"anthropic","model":"m","api_key":"k"}]}}',
            encoding="utf-8",
        )
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.delete("/api/v2/llm/profiles/gone")
        assert resp.status_code == 200
        import json
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["llm"]["profiles"] == []

    def test_delete_default_is_rejected(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.delete("/api/v2/llm/profiles/default")
        assert resp.status_code == 400

    def test_delete_unknown_id_is_idempotent(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        app = create_app(config={}, config_path=cfg_path)
        with TestClient(app) as c:
            resp = c.delete("/api/v2/llm/profiles/never-existed")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
