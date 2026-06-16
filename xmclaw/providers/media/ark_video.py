"""ArkVideoProvider — video generation via Volcengine Ark (火山方舟).

2026-06-16. Covers Doubao Seedance and other Ark video models. Ark
exposes an *async task* API (create → poll → download), distinct from
Replicate's. Uses raw httpx so we don't need the ``volcengine`` SDK as a
hard dependency.

Endpoints (derived from the configured ``base_url`` which already ends
in ``/v3`` or ``/plan/v3``):

* ``POST {base_url}/contents/generations/tasks``  — create task
* ``GET  {base_url}/contents/generations/tasks/{id}`` — poll

Seedance reads ratio / duration / resolution from text-command flags
appended to the prompt (``--ratio 16:9 --duration 5``), not from
top-level JSON fields.
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

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_POLL_INTERVAL = 3.0
_DEFAULT_MAX_POLL_S = 600.0
_TERMINAL = {"succeeded", "failed", "cancelled", "canceled"}


class ArkVideoProvider:
    """Generate videos via Volcengine Ark's content-generation task API.

    Parameters
    ----------
    api_key : str
        Ark API key (used as ``Authorization: Bearer``).
    model : str
        Ark model id or endpoint id, e.g. ``doubao-seedance-2.0`` or an
        ``ep-...`` inference endpoint.
    base_url : str | None
        Ark API base, already including the ``/v3`` (or ``/plan/v3``)
        segment. Defaults to the public Beijing endpoint.
    output_dir : Path | str | None
        Where generated videos are written. Default:
        ``<data_dir>/v2/media/videos``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        output_dir: Path | str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        self._key = api_key.strip()
        self._model = model.strip()
        self._base = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")

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
        t0 = time.perf_counter()

        # Seedance takes ratio/duration as text-command flags on the prompt.
        text = prompt.strip()
        if aspect_ratio:
            text += f" --ratio {aspect_ratio}"
        if duration is not None and duration > 0:
            text += f" --duration {duration}"

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image_path:
            content.append(
                {"type": "image_url", "image_url": {"url": _to_data_uri(image_path)}}
            )

        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        tasks_url = f"{self._base}/contents/generations/tasks"

        async with httpx.AsyncClient() as client:
            # 1. Create task.
            create_resp = await client.post(
                tasks_url,
                headers=headers,
                json={"model": self._model, "content": content},
                timeout=30.0,
            )
            create_resp.raise_for_status()
            created = create_resp.json()
            task_id = created.get("id")
            if not task_id:
                raise RuntimeError(f"Ark returned no task id: {created}")

            logger.info("ark_video.created id=%s model=%s", task_id, self._model)

            # 2. Poll until terminal (or timeout).
            poll_url = f"{tasks_url}/{task_id}"
            status = str(created.get("status") or "queued").lower()
            task = created
            poll_deadline = time.perf_counter() + _DEFAULT_MAX_POLL_S

            while status not in _TERMINAL:
                if time.perf_counter() > poll_deadline:
                    raise RuntimeError(
                        f"Ark task {task_id} timed out after {_DEFAULT_MAX_POLL_S}s"
                    )
                await asyncio.sleep(_DEFAULT_POLL_INTERVAL)
                poll_resp = await client.get(poll_url, headers=headers, timeout=30.0)
                poll_resp.raise_for_status()
                task = poll_resp.json()
                status = str(task.get("status") or "unknown").lower()
                logger.debug("ark_video.poll id=%s status=%s", task_id, status)

            if status in ("failed", "cancelled", "canceled"):
                err = task.get("error") or task.get("message") or "unknown error"
                raise RuntimeError(f"Ark task {task_id} {status}: {err}")

            output_url = _extract_video_url(task)
            if not output_url:
                raise RuntimeError(
                    f"Ark task {task_id} succeeded but no video url found: {task}"
                )

            # 3. Download.
            filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
            dest = self._output_dir / filename
            dl_resp = await client.get(output_url, timeout=180.0)
            dl_resp.raise_for_status()
            dest.write_bytes(dl_resp.content)
            logger.info(
                "ark_video.saved path=%s bytes=%d", dest, len(dl_resp.content),
            )

        return {
            "path": str(dest),
            "output_url": output_url,
            "model": self._model,
            "elapsed_ms": (time.perf_counter() - t0) * 1000.0,
        }


def _extract_video_url(task: dict[str, Any]) -> str | None:
    """Pull the video URL out of an Ark task payload, tolerating shape
    variants (``content.video_url``, ``content.video.url``, a list, …)."""
    content = task.get("content")
    if isinstance(content, dict):
        url = content.get("video_url") or content.get("url")
        if isinstance(url, str) and url:
            return url
        video = content.get("video")
        if isinstance(video, dict):
            url = video.get("url") or video.get("video_url")
            if isinstance(url, str) and url:
                return url
        if isinstance(video, str) and video:
            return video
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            url = first.get("video_url") or first.get("url")
            if isinstance(url, str) and url:
                return url
    # Some responses surface it at the top level.
    url = task.get("video_url")
    return url if isinstance(url, str) and url else None


def _to_data_uri(image_path: str) -> str:
    """Pass through http(s) and data URIs; base64-encode local files."""
    p = image_path.strip()
    if p.startswith(("http://", "https://", "data:")):
        return p
    raw = Path(p).read_bytes()
    mime = mimetypes.guess_type(p)[0] or "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


__all__ = ["ArkVideoProvider"]
