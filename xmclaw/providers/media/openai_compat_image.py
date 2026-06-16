"""OpenAICompatImageProvider — image generation via any OpenAI-compatible
``/images/generations`` endpoint that is NOT OpenAI DALL-E.

2026-06-17 (was ``ark_image.ArkImageProvider``). Covers Volcengine Ark /
Doubao Seedream and generic compat aggregators. These speak the OpenAI
``/images/generations`` shape but reject DALL-E-only params (``quality`` /
``style``) and use their own size set — so :class:`Dalle3Provider` 400s
against them. This backend sends only the portable fields.

``watermark`` is vendor-specific (Volcengine): send it only when the
caller knows the endpoint accepts it, else a strict OpenAI-shape endpoint
may 400 on the unknown field.

Same ``generate(prompt, *, size, quality, style, n)`` signature as
``Dalle3Provider`` so it drops into ``GenerateImageToolProvider``;
``quality`` / ``style`` are accepted and ignored.
"""
from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_DEFAULT_SIZE = "1024x1024"


class OpenAICompatImageProvider:
    """Generate images via an OpenAI-compatible ``/images/generations``
    endpoint (Volcengine Ark / Doubao Seedream / generic compat).

    Parameters
    ----------
    api_key : str
        Bearer token for the endpoint.
    model : str
        Model id, e.g. ``doubao-seedream-3.0`` or an ``ep-...`` endpoint.
    base_url : str
        OpenAI-compatible base, already including ``/v1`` or ``/v3``.
    watermark : bool | None
        When not None, sent as the ``watermark`` body field (Volcengine
        Ark accepts it). Leave None for strict OpenAI-shape endpoints.
    output_dir : Path | str | None
        Where generated images are written. Default
        ``<data_dir>/v2/media/images``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        watermark: bool | None = None,
        output_dir: Path | str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        self._key = api_key.strip()
        self._model = model.strip()
        self._base = base_url.strip().rstrip("/")
        self._watermark = watermark

        if output_dir is None:
            from xmclaw.utils.paths import data_dir
            output_dir = data_dir() / "v2" / "media" / "images"
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        prompt: str,
        *,
        size: str | None = None,
        quality: str | None = None,  # noqa: ARG002 — DALL-E-only, ignored
        style: str | None = None,  # noqa: ARG002 — DALL-E-only, ignored
        n: int = 1,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        effective_size = (size or "").strip() or _DEFAULT_SIZE

        body: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "size": effective_size,
            "n": max(1, int(n)),
            "response_format": "url",
        }
        if self._watermark is not None:
            body["watermark"] = self._watermark
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base}/images/generations"

        logger.info(
            "compat_image.generate model=%s size=%s prompt=%r",
            self._model, effective_size, prompt[:80],
        )

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=body, timeout=180.0)
            resp.raise_for_status()
            data = resp.json()

            items = data.get("data")
            if not isinstance(items, list) or not items:
                raise RuntimeError(f"image endpoint returned no data: {data}")

            paths: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                dest = self._output_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
                img_url = item.get("url")
                b64 = item.get("b64_json")
                if isinstance(img_url, str) and img_url:
                    r = await client.get(img_url, timeout=120.0)
                    r.raise_for_status()
                    dest.write_bytes(r.content)
                elif isinstance(b64, str) and b64:
                    dest.write_bytes(base64.b64decode(b64))
                else:
                    continue
                paths.append(str(dest))
                logger.info("compat_image.saved path=%s", dest)

        if not paths:
            raise RuntimeError("image generation produced no downloadable output")

        return {
            "paths": paths,
            "revised_prompts": [""] * len(paths),
            "size": effective_size,
            "quality": None,
            "style": None,
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }


__all__ = ["OpenAICompatImageProvider"]
