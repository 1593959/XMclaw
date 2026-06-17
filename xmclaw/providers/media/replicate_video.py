"""ReplicateVideoProvider — video generation via Replicate API.

2026-06-15. Uses raw HTTP (httpx) so we don't need the ``replicate``
package as a hard dependency.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_REPLICATE_API = "https://api.replicate.com/v1"
_DEFAULT_MODEL = "stability-ai/stable-video-diffusion-img2vid-xt-1-1"
_DEFAULT_POLL_INTERVAL = 3.0
_DEFAULT_MAX_POLL_S = 300.0


class ReplicateVideoProvider:
    """Generate videos via Replicate.

    Parameters
    ----------
    api_token : str
        Replicate API token (starts with ``r8_...``).
    model_version : str
        Replicate model identifier, e.g.
        ``"stability-ai/stable-video-diffusion-img2vid-xt-1-1"``.
    output_dir : Path | str | None
        Where generated videos are written. Default:
        ``<data_dir>/v2/media/videos``.
    """

    def __init__(
        self,
        *,
        api_token: str,
        model_version: str = _DEFAULT_MODEL,
        output_dir: Path | str | None = None,
    ) -> None:
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("api_token is required")
        self._token = api_token.strip()
        self._model = model_version.strip() or _DEFAULT_MODEL

        if output_dir is None:
            from xmclaw.utils.paths import data_dir
            output_dir = data_dir() / "v2" / "media" / "videos"
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        prompt: str,
        *,
        image_path: str | None = None,
        duration: int | None = None,
        aspect_ratio: str | None = None,
    ) -> dict[str, Any]:
        """Generate a video.

        Parameters
        ----------
        prompt : str
            Text description of the desired video.
        image_path : str | None
            Optional starting image path. Some Replicate models require
            an image (img2vid); others accept text-only (text2vid).
        duration : int | None
            Target duration in seconds (model-dependent; may be ignored).
        aspect_ratio : str | None
            e.g. "16:9", "9:16", "1:1" (model-dependent).

        Returns
        -------
        dict with keys:
            path : str
                Local filesystem path to the downloaded video.
            output_url : str
                The Replicate output URL.
            model : str
                The model version used.
            elapsed_ms : float
                Total wall-clock time.
        """
        t0 = time.perf_counter()

        # Build input payload — model-specific fields are passed through.
        input_payload: dict[str, Any] = {"prompt": prompt}
        if image_path:
            input_payload["image"] = image_path
        if duration is not None:
            input_payload["duration"] = duration
        if aspect_ratio:
            input_payload["aspect_ratio"] = aspect_ratio

        headers = {
            "Authorization": f"Token {self._token}",
            "Content-Type": "application/json",
            "Prefer": "wait",  # Ask Replicate to wait if possible.
        }

        async with httpx.AsyncClient() as client:
            # 1. Create prediction.
            create_resp = await client.post(
                f"{_REPLICATE_API}/predictions",
                headers=headers,
                json={"version": self._model, "input": input_payload},
                timeout=30.0,
            )
            from xmclaw.utils.http_errors import raise_for_vendor_error
            raise_for_vendor_error(create_resp, "Replicate create prediction")
            pred = create_resp.json()
            pred_id = pred.get("id")
            if not pred_id:
                raise RuntimeError(f"Replicate returned no prediction id: {pred}")

            logger.info(
                "replicate_video.created id=%s model=%s", pred_id, self._model,
            )

            # 2. Poll until completion (or timeout).
            status = pred.get("status", "starting")
            output_url: str | None = None
            poll_deadline = time.perf_counter() + _DEFAULT_MAX_POLL_S

            while status not in ("succeeded", "failed", "canceled"):
                if time.perf_counter() > poll_deadline:
                    raise RuntimeError(
                        f"Replicate prediction {pred_id} timed out after "
                        f"{_DEFAULT_MAX_POLL_S}s"
                    )
                await asyncio.sleep(_DEFAULT_POLL_INTERVAL)
                poll_resp = await client.get(
                    f"{_REPLICATE_API}/predictions/{pred_id}",
                    headers=headers,
                    timeout=30.0,
                )
                raise_for_vendor_error(poll_resp, "Replicate poll prediction")
                pred = poll_resp.json()
                status = pred.get("status", "unknown")
                logger.debug(
                    "replicate_video.poll id=%s status=%s", pred_id, status,
                )

            if status == "failed":
                error_msg = pred.get("error", "unknown error")
                raise RuntimeError(
                    f"Replicate prediction {pred_id} failed: {error_msg}"
                )

            # 3. Extract output URL.
            output = pred.get("output")
            if isinstance(output, str):
                output_url = output
            elif isinstance(output, list) and output:
                output_url = str(output[0])
            elif isinstance(output, dict):
                output_url = output.get("video") or output.get("url")
            else:
                raise RuntimeError(
                    f"Replicate prediction {pred_id} succeeded but "
                    f"returned unexpected output: {output}"
                )

            if not output_url:
                raise RuntimeError(
                    f"Replicate prediction {pred_id} succeeded but "
                    f"no output URL was found"
                )

            # 4. Download.
            ext = ".mp4"
            filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
            dest = self._output_dir / filename
            dl_resp = await client.get(output_url, timeout=120.0)
            dl_resp.raise_for_status()
            dest.write_bytes(dl_resp.content)
            logger.info(
                "replicate_video.saved path=%s bytes=%d", dest, len(dl_resp.content),
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "path": str(dest),
            "output_url": output_url,
            "model": self._model,
            "elapsed_ms": elapsed_ms,
        }


__all__ = ["ReplicateVideoProvider"]
