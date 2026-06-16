"""Dalle3Provider — OpenAI DALL-E 3 image generation.

2026-06-15. Thin wrapper around ``openai.images.generate``.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_INSTALL_HINT = (
    "Dalle3Provider needs the ``openai`` package. "
    "Install with: pip install openai"
)

# OpenAI DALL-E 3 valid sizes
_VALID_SIZES = {"1024x1024", "1792x1024", "1024x1792"}
_VALID_QUALITIES = {"standard", "hd"}
_VALID_STYLES = {"vivid", "natural"}


class Dalle3Provider:
    """Generate images via OpenAI DALL-E 3.

    Parameters
    ----------
    api_key : str
        OpenAI API key.
    base_url : str | None
        Override for OpenAI-compatible endpoints (e.g. Azure).
    model : str
        Default ``"dall-e-3"``. Forward-compat for DALL-E 4 etc.
    default_size : str
        Default ``"1024x1024"``.
    default_quality : str
        Default ``"standard"``.
    default_style : str
        Default ``"vivid"``.
    output_dir : Path | str | None
        Where generated images are written. Default:
        ``<data_dir>/v2/media/images``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str = "dall-e-3",
        default_size: str = "1024x1024",
        default_quality: str = "standard",
        default_style: str = "vivid",
        output_dir: Path | str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        self._api_key = api_key.strip()
        self._base_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
        self._model = model.strip() or "dall-e-3"
        self._default_size = default_size if default_size in _VALID_SIZES else "1024x1024"
        self._default_quality = default_quality if default_quality in _VALID_QUALITIES else "standard"
        self._default_style = default_style if default_style in _VALID_STYLES else "vivid"

        if output_dir is None:
            from xmclaw.utils.paths import data_dir
            output_dir = data_dir() / "v2" / "media" / "images"
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def generate(
        self,
        prompt: str,
        *,
        size: str | None = None,
        quality: str | None = None,
        style: str | None = None,
        n: int = 1,
    ) -> dict[str, Any]:
        """Generate one or more images.

        Returns
        -------
        dict with keys:
            paths : list[str]
                Local filesystem paths to the downloaded images.
            revised_prompts : list[str]
                The prompt DALL-E actually used (OpenAI may revise it
                for safety / quality).
            size, quality, style : str
                The effective values used.
            elapsed_ms : float
                Total wall-clock time.
        """
        t0 = time.perf_counter()
        client = self._get_client()

        effective_size = size if size in _VALID_SIZES else self._default_size
        effective_quality = quality if quality in _VALID_QUALITIES else self._default_quality
        effective_style = style if style in _VALID_STYLES else self._default_style

        logger.info(
            "dalle3.generate prompt=%r size=%s quality=%s style=%s n=%d",
            prompt[:80], effective_size, effective_quality, effective_style, n,
        )

        try:
            resp = await client.images.generate(
                model=self._model,
                prompt=prompt,
                size=effective_size,
                quality=effective_quality,
                style=effective_style,
                n=n,
                response_format="url",
            )
        except Exception as exc:
            logger.warning("dalle3.generate failed: %s", exc)
            raise

        data_list = getattr(resp, "data", []) or []
        if not data_list:
            raise RuntimeError("OpenAI returned empty image data list")

        paths: list[str] = []
        revised_prompts: list[str] = []
        async with httpx.AsyncClient() as http:
            for item in data_list:
                url = getattr(item, "url", None) or ""
                revised = getattr(item, "revised_prompt", None) or ""
                if not url:
                    continue
                revised_prompts.append(revised)
                # Download and save locally.
                ext = ".png"  # DALL-E returns PNG.
                filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
                dest = self._output_dir / filename
                try:
                    r = await http.get(url, timeout=60.0)
                    r.raise_for_status()
                    dest.write_bytes(r.content)
                    paths.append(str(dest))
                    logger.info("dalle3.saved path=%s bytes=%d", dest, len(r.content))
                except Exception as exc:
                    logger.warning("dalle3.download_failed url=%s err=%s", url, exc)
                    raise RuntimeError(f"failed to download image: {exc}") from exc

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "paths": paths,
            "revised_prompts": revised_prompts,
            "size": effective_size,
            "quality": effective_quality,
            "style": effective_style,
            "elapsed_ms": elapsed_ms,
        }


__all__ = ["Dalle3Provider"]
