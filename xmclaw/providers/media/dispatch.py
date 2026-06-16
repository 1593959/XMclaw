"""Media-backend dispatch — protocol-based, not vendor-bespoke.

2026-06-17. Instead of an if/else tower per vendor in the factory, this
module maps a media profile (api_key + model + base_url) to the backend
that speaks the right wire protocol. Adding a vendor = a branch here, not
a new factory edit.

Protocols in play:

* image  — OpenAI-compatible sync (DALL-E / Seedream / generic compat) +
  MiniMax native sync envelope.
* video  — async submit+poll task (Replicate / Ark / MiniMax).

Each builder returns a backend exposing the ``generate(...)`` coroutine the
matching ToolProvider expects, or ``None`` when nothing resolves.
"""
from __future__ import annotations

from typing import Any

from xmclaw.utils.vendor_detect import detect_media_vendor

__all__ = ["build_image_backend", "build_video_backend"]


def build_image_backend(
    *, api_key: str, model: str, base_url: str | None, provider: str | None = None,
) -> Any | None:
    """Return an image-gen backend for the given profile, or None."""
    vendor = detect_media_vendor(model, base_url)

    if vendor == "minimax":
        from xmclaw.providers.media.minimax_image import MiniMaxImageProvider
        return MiniMaxImageProvider(
            api_key=api_key, model=model, base_url=base_url or "",
        )

    if vendor == "openai":
        # OpenAI proper — DALL-E SDK path (sends quality/style).
        from xmclaw.providers.media.dalle3 import Dalle3Provider
        return Dalle3Provider(api_key=api_key, base_url=base_url, model=model)

    # ark + openai_compat → portable /images/generations. Volcengine Ark
    # accepts watermark=false; generic compat hosts may not, so only send
    # it for ark.
    from xmclaw.providers.media.openai_compat_image import (
        OpenAICompatImageProvider,
    )
    return OpenAICompatImageProvider(
        api_key=api_key,
        model=model,
        base_url=base_url or "https://ark.cn-beijing.volces.com/api/v3",
        watermark=False if vendor == "ark" else None,
    )


def build_video_backend(
    *,
    api_key: str,
    model: str,
    base_url: str | None,
    provider: str | None = None,
) -> Any | None:
    """Return a video-gen backend for the given profile, or None."""
    vendor = detect_media_vendor(model, base_url)

    if vendor == "replicate" or provider == "replicate":
        from xmclaw.providers.media.replicate_video import ReplicateVideoProvider
        return ReplicateVideoProvider(api_token=api_key, model_version=model)

    if vendor == "minimax":
        from xmclaw.providers.media.minimax_video import MiniMaxVideoProvider
        return MiniMaxVideoProvider(
            api_key=api_key, model=model, base_url=base_url,
        )

    # ark + openai_compat → Ark's submit+poll task shape (the de-facto
    # protocol Chinese compat video hosts speak).
    from xmclaw.providers.media.ark_video import ArkVideoProvider
    return ArkVideoProvider(api_key=api_key, model=model, base_url=base_url)
