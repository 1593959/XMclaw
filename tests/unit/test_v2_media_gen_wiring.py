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


def test_vendor_detection_table() -> None:
    from xmclaw.utils.vendor_detect import detect_media_vendor as d
    assert d("doubao-seedance-2.0", "https://ark.cn-beijing.volces.com/api/v3") == "ark"
    assert d("doubao-seedream-3.0", "https://ark.example.com/v1") == "ark"
    assert d("MiniMax-Hailuo-2.3", "https://api.minimax.io/v1") == "minimax"
    assert d("image-01", "https://api.minimaxi.com/v1") == "minimax"
    assert d("speech-02-hd", "https://api.minimax.io/v1") == "minimax"
    assert d("anything", "https://api.replicate.com/v1") == "replicate"
    assert d("dall-e-3", "https://api.openai.com/v1") == "openai"
    assert d("tts-1-hd", "https://api.openai.com/v1") == "openai"
    # Unknown model on an unknown host → portable OpenAI-compat fallback.
    assert d("flux-pro", "https://apihub.agnes-ai.com/v1") == "openai_compat"


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


def test_image_backend_dalle_for_openai_models() -> None:
    from xmclaw.providers.media.dalle3 import Dalle3Provider
    from xmclaw.providers.tool.generate_image import GenerateImageToolProvider

    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai", "model": "dall-e-3",
         "api_key": "sk-img", "base_url": "https://api.openai.com/v1"},
    ]}}
    providers = _build_media_tool_providers(cfg)
    img = next(p for p in providers if isinstance(p, GenerateImageToolProvider))
    assert isinstance(img._provider, Dalle3Provider)


def test_image_backend_compat_for_seedream_on_compat_host() -> None:
    # Doubao Seedream on Ark must NOT use Dalle3Provider — the DALL-E-only
    # quality/style params 400 there. Route to the portable backend, with
    # Volcengine's watermark=false opt-in set.
    from xmclaw.providers.media.openai_compat_image import (
        OpenAICompatImageProvider,
    )
    from xmclaw.providers.tool.generate_image import GenerateImageToolProvider

    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "doubao-seedream-3.0",
         "api_key": "sk-img", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    ]}}
    providers = _build_media_tool_providers(cfg)
    img = next(p for p in providers if isinstance(p, GenerateImageToolProvider))
    assert isinstance(img._provider, OpenAICompatImageProvider)
    assert img._provider._model == "doubao-seedream-3.0"
    assert img._provider._watermark is False  # ark → watermark opt-in


def test_image_backend_minimax_native() -> None:
    from xmclaw.providers.media.minimax_image import MiniMaxImageProvider
    from xmclaw.providers.tool.generate_image import GenerateImageToolProvider

    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "image-01",
         "api_key": "sk-mm", "base_url": "https://api.minimax.io/v1"},
    ]}}
    providers = _build_media_tool_providers(cfg)
    img = next(p for p in providers if isinstance(p, GenerateImageToolProvider))
    assert isinstance(img._provider, MiniMaxImageProvider)


def test_image_backend_generic_compat_no_watermark() -> None:
    # A generic OpenAI-compat host (not Ark) must NOT get watermark=false —
    # strict OpenAI-shape endpoints 400 on unknown body fields.
    from xmclaw.providers.media.openai_compat_image import (
        OpenAICompatImageProvider,
    )
    from xmclaw.providers.tool.generate_image import GenerateImageToolProvider

    cfg = {"llm": {"profiles": [
        {"id": "img", "provider": "openai_compat", "model": "flux-pro",
         "api_key": "sk-x", "base_url": "https://apihub.agnes-ai.com/v1",
         "capabilities": ["image_gen"]},
    ]}}
    providers = _build_media_tool_providers(cfg)
    img = next(p for p in providers if isinstance(p, GenerateImageToolProvider))
    assert isinstance(img._provider, OpenAICompatImageProvider)
    assert img._provider._watermark is None


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


def test_build_media_providers_ark_video_from_profile() -> None:
    # A video_gen model on a non-Replicate OpenAI-compat host (Volcengine
    # Ark / Doubao Seedance) must still light up generate_video — backed by
    # ArkVideoProvider, not Replicate. Regression: previously the tool only
    # appeared for Replicate, so the agent fell back to ad-hoc skills.
    from xmclaw.providers.media.ark_video import ArkVideoProvider
    from xmclaw.providers.tool.generate_video import GenerateVideoToolProvider

    cfg = {"llm": {"profiles": [
        {"id": "vid", "provider": "openai_compat", "model": "doubao-seedance-2.0",
         "api_key": "sk-vid", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    ]}}
    providers = _build_media_tool_providers(cfg)
    vid_providers = [
        p for p in providers
        if isinstance(p, GenerateVideoToolProvider)
    ]
    assert len(vid_providers) == 1
    backend = vid_providers[0]._provider
    assert isinstance(backend, ArkVideoProvider)
    assert backend._model == "doubao-seedance-2.0"
    assert backend._base == "https://ark.cn-beijing.volces.com/api/v3"


def test_generate_video_in_catalogue_for_ark_profile() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "vid", "provider": "openai_compat", "model": "doubao-seedance-2.0",
         "api_key": "sk-vid", "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3"},
    ]}}
    tools = build_tools_from_config(cfg)
    assert "generate_video" in _tool_names(tools)


def test_video_backend_minimax_from_profile() -> None:
    from xmclaw.providers.media.minimax_video import MiniMaxVideoProvider
    from xmclaw.providers.tool.generate_video import GenerateVideoToolProvider

    cfg = {"llm": {"profiles": [
        {"id": "vid", "provider": "openai_compat", "model": "MiniMax-Hailuo-2.3",
         "api_key": "sk-mm", "base_url": "https://api.minimax.io/v1"},
    ]}}
    providers = _build_media_tool_providers(cfg)
    vid = next(p for p in providers if isinstance(p, GenerateVideoToolProvider))
    assert isinstance(vid._provider, MiniMaxVideoProvider)
    assert vid._provider._model == "MiniMax-Hailuo-2.3"


# ── audio_out profile → remote TTS wiring ───────────────────────────


def test_scan_finds_audio_out_profile() -> None:
    cfg = {"llm": {"profiles": [
        {"id": "tts", "provider": "openai_compat", "model": "speech-02-hd",
         "api_key": "sk-tts", "base_url": "https://api.minimax.io/v1"},
    ]}}
    found = _scan_media_profiles(cfg)
    assert "audio_out" in found
    assert found["audio_out"]["model"] == "speech-02-hd"


def test_tts_dispatch_minimax_vs_openai_vs_volcengine() -> None:
    from xmclaw.providers.voice.dispatch import build_tts_backend
    from xmclaw.providers.voice.minimax_tts import MiniMaxTTS
    from xmclaw.providers.voice.openai_tts import OpenAICompatTTS

    mm = build_tts_backend(
        api_key="k", model="speech-02-hd", base_url="https://api.minimax.io/v1")
    assert isinstance(mm, MiniMaxTTS)

    oa = build_tts_backend(
        api_key="k", model="tts-1", base_url="https://api.openai.com/v1")
    assert isinstance(oa, OpenAICompatTTS)

    # Volcengine seed-tts (native binary API) has no portable backend → None,
    # so the factory falls back to EdgeTTS.
    vol = build_tts_backend(
        api_key="k", model="seed-tts-2.0",
        base_url="https://ark.cn-beijing.volces.com/api/v3")
    assert vol is None


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
