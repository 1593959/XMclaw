"""Phase 11 — generate_image / generate_video tool wiring.

The media *backends* (Dalle3Provider / ReplicateVideoProvider) and the
*tool wrappers* (GenerateImage/VideoToolProvider) already exist; this
file pins the FACTORY wiring that makes them appear in the agent's tool
catalogue when — and only when — a matching model is configured.

Contract:
  * A profile tagged (or name-inferred) ``image_gen`` → ``generate_image``
    is registered with a real backend.
  * No image-gen model configured → ``generate_image`` is absent (clean
    catalogue, not a tool that always fails).
  * Same shape for ``video_gen`` via a ``media.replicate`` block.
"""
from __future__ import annotations

from xmclaw.daemon.factory import (
    _build_media_tool_providers,
    _infer_capabilities_from_model,
    _scan_media_profiles,
    build_tools_from_config,
)


def _tool_names(provider) -> set[str]:
    return {s.name for s in provider.list_tools()}


# ── capability inference recognizes the gen-model families ──────────


def test_inference_tags_image_and_video_and_audio_families() -> None:
    assert "image_gen" in _infer_capabilities_from_model("doubao-seedream-3.0")
    assert "image_gen" in _infer_capabilities_from_model("dall-e-3")
    assert "image_gen" in _infer_capabilities_from_model("flux-pro")
    assert "video_gen" in _infer_capabilities_from_model("doubao-seedance-2.0")
    assert "video_gen" in _infer_capabilities_from_model("kling-v1")
    assert "audio_out" in _infer_capabilities_from_model("seed-tts-2.0")


# ── _scan_media_profiles resolves the backend config ────────────────


def test_scan_finds_image_gen_profile_by_name_inference() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "doubao-seedream-3.0",
         "api_key": "sk-img", "base_url": "https://ark.example.com/v1"},
        {"id": "chat", "provider": "openai_compat", "model": "agnes-2.0-flash",
         "api_key": "sk-chat", "base_url": "https://x/v1"},
    ]}}
    found = _scan_media_profiles(cfg)
    assert "image_gen" in found
    assert found["image_gen"]["api_key"] == "sk-img"
    assert found["image_gen"]["model"] == "doubao-seedream-3.0"
    # The chat model must NOT be picked up as a media backend.
    assert "video_gen" not in found


def test_scan_respects_explicit_capability_tag() -> None:
    # A model whose NAME the heuristic can't classify, but the user
    # tagged image_gen in the UI, should still resolve.
    cfg = {"llm": {"profiles": [
        {"id": "x", "provider": "openai_compat", "model": "mystery-canvas-9000",
         "api_key": "sk-x", "base_url": "https://x/v1",
         "capabilities": ["image_gen"]},
    ]}}
    found = _scan_media_profiles(cfg)
    assert "image_gen" in found
    assert found["image_gen"]["model"] == "mystery-canvas-9000"


def test_scan_skips_disabled_profiles() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "dall-e-3",
         "api_key": "sk-img", "base_url": "https://x/v1", "enabled": False},
    ]}}
    assert _scan_media_profiles(cfg) == {}


def test_scan_skips_profile_without_api_key() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "dall-e-3",
         "base_url": "https://x/v1"},
    ]}}
    assert _scan_media_profiles(cfg) == {}


# ── _build_media_tool_providers builds the tool wrappers ────────────


def test_build_media_providers_registers_generate_image() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "doubao-seedream-3.0",
         "api_key": "sk-img", "base_url": "https://ark.example.com/v1"},
    ]}}
    providers = _build_media_tool_providers(cfg)
    names: set[str] = set()
    for p in providers:
        names |= _tool_names(p)
    assert "generate_image" in names


def test_build_media_providers_replicate_video_from_media_block() -> None:
    cfg = {
        "llm": {"profiles": []},
        "media": {"replicate": {"api_token": "r8_xxx", "model": "owner/model"}},
    }
    providers = _build_media_tool_providers(cfg)
    names: set[str] = set()
    for p in providers:
        names |= _tool_names(p)
    assert "generate_video" in names


# ── end-to-end through build_tools_from_config ─────────────────────


def test_generate_image_in_catalogue_when_configured() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "doubao-seedream-3.0",
         "api_key": "sk-img", "base_url": "https://ark.example.com/v1"},
    ]}}
    tools = build_tools_from_config(cfg)
    assert "generate_image" in _tool_names(tools)


def test_generate_image_absent_when_no_image_model() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "chat", "provider": "openai_compat", "model": "agnes-2.0-flash",
         "api_key": "sk-chat", "base_url": "https://x/v1"},
    ]}}
    tools = build_tools_from_config(cfg)
    assert "generate_image" not in _tool_names(tools)
