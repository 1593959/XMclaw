"""MiniMaxVideoProvider — video generation via MiniMax (海螺 Hailuo).

2026-06-16. MiniMax uses a *three-call* async flow, distinct from
Replicate / Ark:

* ``POST {base}/video_generation``            — create task → ``task_id``
* ``GET  {base}/query/video_generation?task_id=`` — poll → ``status`` + ``file_id``
* ``GET  {base}/files/retrieve?file_id=``     — resolve ``download_url``

Same ``generate(prompt, *, image_path, duration, aspect_ratio)`` signature
as the other video backends so it drops into ``GenerateVideoToolProvider``.
Auth is ``Authorization: Bearer {key}``; some legacy deployments also want
a ``GroupId`` query param on file ops, so it's sent when configured.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.minimax.io/v1"
_DEFAULT_MODEL = "MiniMax-Hailuo-2.3"
_DEFAULT_POLL_INTERVAL = 5.0
_DEFAULT_MAX_POLL_S = 600.0
# MiniMax status strings are capitalised; treat anything else as terminal-fail.
_TERMINAL_OK = "success"
_TERMINAL_FAIL = {"fail", "failed", "cancelled", "canceled", "unknown"}


class MiniMaxVideoProvider:
    """Generate videos via MiniMax's async video-generation API.

    Parameters
    ----------
    api_key : str
        MiniMax API key (``Authorization: Bearer``).
    model : str
        e.g. ``MiniMax-Hailuo-2.3`` / ``MiniMax-Hailuo-02`` / ``T2V-01``.
    base_url : str | None
        API base ending in ``/v1``. Defaults to the global host.
    group_id : str | None
        Optional legacy ``GroupId`` query param for file ops.
    output_dir : Path | str | None
        Where videos are written. Default ``<data_dir>/v2/media/videos``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        group_id: str | None = None,
        output_dir: Path | str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        self._key = api_key.strip()
        self._model = (model or _DEFAULT_MODEL).strip()
        self._base = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")
        self._group_id = (group_id or "").strip() or None

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
        aspect_ratio: str | None = None,  # noqa: ARG002 — MiniMax uses resolution
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

        body: dict[str, Any] = {"model": self._model, "prompt": prompt}
        if duration is not None and duration > 0:
            body["duration"] = duration
        if image_path:
            # MiniMax image-to-video takes a first frame (url or data URI).
            body["first_frame_image"] = _to_data_uri(image_path)

        async with httpx.AsyncClient() as client:
            # 1. Create task.
            create = await client.post(
                f"{self._base}/video_generation",
                headers=headers, json=body, timeout=30.0,
            )
            create.raise_for_status()
            created = create.json()
            _raise_on_base_resp(created, "create video task")
            task_id = created.get("task_id")
            if not task_id:
                raise RuntimeError(f"MiniMax returned no task_id: {created}")

            logger.info("minimax_video.created task=%s model=%s", task_id, self._model)

            # 2. Poll.
            file_id: str | None = None
            poll_deadline = time.perf_counter() + _DEFAULT_MAX_POLL_S
            params = {"task_id": task_id}
            while True:
                if time.perf_counter() > poll_deadline:
                    raise RuntimeError(
                        f"MiniMax task {task_id} timed out after {_DEFAULT_MAX_POLL_S}s"
                    )
                await asyncio.sleep(_DEFAULT_POLL_INTERVAL)
                q = await client.get(
                    f"{self._base}/query/video_generation",
                    headers=headers, params=params, timeout=30.0,
                )
                q.raise_for_status()
                qj = q.json()
                status = str(qj.get("status") or "").strip().lower()
                logger.debug("minimax_video.poll task=%s status=%s", task_id, status)
                if status == _TERMINAL_OK:
                    file_id = qj.get("file_id")
                    break
                if status in _TERMINAL_FAIL:
                    raise RuntimeError(
                        f"MiniMax task {task_id} failed (status={status}): {qj}"
                    )

            if not file_id:
                raise RuntimeError(f"MiniMax task {task_id} succeeded but no file_id")

            # 3. Resolve download URL.
            fparams: dict[str, str] = {"file_id": str(file_id)}
            if self._group_id:
                fparams["GroupId"] = self._group_id
            fr = await client.get(
                f"{self._base}/files/retrieve",
                headers=headers, params=fparams, timeout=30.0,
            )
            fr.raise_for_status()
            frj = fr.json()
            _raise_on_base_resp(frj, "retrieve file")
            output_url = _extract_download_url(frj)
            if not output_url:
                raise RuntimeError(f"MiniMax file {file_id} has no download_url: {frj}")

            # 4. Download.
            dest = self._output_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
            dl = await client.get(output_url, timeout=180.0)
            dl.raise_for_status()
            dest.write_bytes(dl.content)
            logger.info("minimax_video.saved path=%s bytes=%d", dest, len(dl.content))

        return {
            "path": str(dest),
            "output_url": output_url,
            "model": self._model,
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }


def _raise_on_base_resp(payload: dict[str, Any], what: str) -> None:
    """MiniMax wraps errors in ``base_resp.status_code != 0``."""
    br = payload.get("base_resp")
    if isinstance(br, dict):
        code = br.get("status_code")
        if isinstance(code, int) and code != 0:
            raise RuntimeError(
                f"MiniMax {what} error {code}: {br.get('status_msg') or ''}"
            )


def _extract_download_url(payload: dict[str, Any]) -> str | None:
    file_obj = payload.get("file")
    if isinstance(file_obj, dict):
        url = file_obj.get("download_url") or file_obj.get("url")
        if isinstance(url, str) and url:
            return url
    url = payload.get("download_url")
    return url if isinstance(url, str) and url else None


def _to_data_uri(image_path: str) -> str:
    p = image_path.strip()
    if p.startswith(("http://", "https://", "data:")):
        return p
    raw = Path(p).read_bytes()
    mime = mimetypes.guess_type(p)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


__all__ = ["MiniMaxVideoProvider"]
