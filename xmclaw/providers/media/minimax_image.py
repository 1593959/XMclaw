"""MiniMaxImageProvider — image generation via MiniMax (image-01).

2026-06-16. MiniMax image generation is *synchronous* (no task/poll) but
its response shape differs from OpenAI's ``/images/generations``: images
come back in ``data.image_urls`` (a flat list of strings) or
``data.image_base64``, and errors surface via ``base_resp.status_code``.

Same ``generate(prompt, *, size, quality, style, n)`` signature as the
other image backends so it drops into ``GenerateImageToolProvider``;
``size`` is mapped to MiniMax's ``aspect_ratio``, ``quality`` / ``style``
are accepted and ignored.
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

_DEFAULT_BASE_URL = "https://api.minimax.io/v1"
_DEFAULT_MODEL = "image-01"

# OpenAI-style WxH → MiniMax aspect_ratio. Unknown sizes fall back to 1:1.
_SIZE_TO_RATIO = {
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1152x864": "4:3",
    "864x1152": "3:4",
}


class MiniMaxImageProvider:
    """Generate images via MiniMax's ``/image_generation`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        output_dir: Path | str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        self._key = api_key.strip()
        self._model = (model or _DEFAULT_MODEL).strip()
        self._base = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")

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
        quality: str | None = None,  # noqa: ARG002 — not a MiniMax param
        style: str | None = None,  # noqa: ARG002 — not a MiniMax param
        n: int = 1,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        ratio = _SIZE_TO_RATIO.get((size or "").strip(), "1:1")
        body = {
            "model": self._model,
            "prompt": prompt,
            "aspect_ratio": ratio,
            "n": max(1, int(n)),
            "response_format": "url",
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "minimax_image.generate model=%s ratio=%s prompt=%r",
            self._model, ratio, prompt[:80],
        )

        from xmclaw.utils.http_errors import raise_for_vendor_error

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/image_generation",
                headers=headers, json=body, timeout=180.0,
            )
            raise_for_vendor_error(resp, f"MiniMax image_generation (model={self._model})")
            data = resp.json()
            _raise_on_base_resp(data)

            payload = data.get("data")
            if not isinstance(payload, dict):
                raise RuntimeError(f"MiniMax image returned no data: {data}")
            urls = payload.get("image_urls")
            b64s = payload.get("image_base64")

            paths: list[str] = []
            if isinstance(urls, list) and urls:
                for u in urls:
                    if not isinstance(u, str) or not u:
                        continue
                    dest = self._output_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
                    r = await client.get(u, timeout=120.0)
                    r.raise_for_status()
                    dest.write_bytes(r.content)
                    paths.append(str(dest))
                    logger.info("minimax_image.saved path=%s", dest)
            elif isinstance(b64s, list) and b64s:
                for b in b64s:
                    if not isinstance(b, str) or not b:
                        continue
                    dest = self._output_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
                    dest.write_bytes(base64.b64decode(b))
                    paths.append(str(dest))

        if not paths:
            raise RuntimeError("MiniMax image generation produced no output")

        return {
            "paths": paths,
            "revised_prompts": [""] * len(paths),
            "size": size or "1024x1024",
            "quality": None,
            "style": None,
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }


def _raise_on_base_resp(payload: dict[str, Any]) -> None:
    br = payload.get("base_resp")
    if isinstance(br, dict):
        code = br.get("status_code")
        if isinstance(code, int) and code != 0:
            raise RuntimeError(
                f"MiniMax image error {code}: {br.get('status_msg') or ''}"
            )


__all__ = ["MiniMaxImageProvider"]
