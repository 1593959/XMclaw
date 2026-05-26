"""WS user-frame image intake — decode data: URLs to disk paths.

Lifted out of ``app.py``'s WebSocket user-frame handler 2026-05-26
so the save loop is callable from tests. The original inline block
silently dropped every uploaded image for weeks because a missing
``time`` import threw ``NameError`` inside a broad
``try/except Exception`` — daemon log evidence:

    ws.user_frame: image save failed: NameError("name 'time' is
    not defined")

The user-visible symptom was "model can't see chat images even
though I attached one" — the file never reached disk, so
``user_image_paths`` was always empty by the time it hit run_turn.

Keeping the helper here (alongside the WS handler) instead of in
``utils/`` so the import surface for the WS path stays local and
the function can be exercised by a unit test without spinning up
the FastAPI app.
"""
from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.utils.log import get_logger

log = get_logger(__name__)


# Each user message can carry at most this many image attachments.
# Matches the ``raw_images[:8]`` slice the WS handler used inline.
MAX_IMAGES_PER_FRAME = 8

# Per-attachment size cap. Larger blobs are rejected; the model
# wouldn't accept them anyway and the WS frame would balloon.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# data:image/foo;base64 → file extension. Anything not on the list
# falls through to ``.bin`` — the file still saves, the LLM
# translator just may not recognise the MIME.
_EXT_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
}


def save_user_frame_images(
    raw_images: Any,
    uploads_dir: Path,
) -> list[str]:
    """Decode the ``images`` field of a WS user frame to disk.

    ``raw_images`` is whatever the WS frame had under that key —
    usually ``list[str]`` of ``data:<mime>;base64,<payload>`` URLs,
    but we accept anything and return ``[]`` for non-list /
    malformed input rather than raising.

    Returns the list of absolute paths of saved files (str form,
    matching the legacy contract that fed ``run_turn(user_images=
    tuple(...))``). Bad / oversized entries are skipped with a
    warn-log; we never raise here so a single bogus attachment
    can't take down the whole turn.

    On a clean run with N valid data URLs, ``uploads_dir`` ends
    up with N files and the returned list has N matching paths.
    """
    if not isinstance(raw_images, list):
        log.debug(
            "ws.user_frame: images field is not a list (type=%s)",
            type(raw_images).__name__,
        )
        return []

    log.debug("ws.user_frame: images_count=%d", len(raw_images))
    uploads_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[str] = []

    for entry in raw_images[:MAX_IMAGES_PER_FRAME]:
        if not isinstance(entry, str):
            log.debug("ws.user_frame: skip non-str image entry")
            continue
        if not entry.startswith("data:"):
            log.debug(
                "ws.user_frame: skip non-data image entry: %s...",
                entry[:40],
            )
            continue
        try:
            header, payload_b64 = entry.split(",", 1)
            # e.g. "data:image/png;base64"
            meta = header[len("data:"):]
            mime = (meta.split(";")[0] or "image/png").lower()
            ext = _EXT_MAP.get(mime, ".bin")
            raw_bytes = base64.b64decode(payload_b64)
            if len(raw_bytes) > MAX_IMAGE_BYTES:
                log.warning(
                    "ws.user_frame: reject oversized image: %s bytes",
                    len(raw_bytes),
                )
                continue
            out = uploads_dir / (
                f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
            )
            out.write_bytes(raw_bytes)
            out_paths.append(str(out))
            log.debug("ws.user_frame: saved image to %s", out)
        except Exception as exc:  # noqa: BLE001 — skip bad blobs
            log.warning("ws.user_frame: image save failed: %s", exc)
            # 2026-05-26 (audit A3): count this swallow so the doctor
            # check can surface "this is firing every turn" before
            # it becomes a multi-week silent failure (chat-b3c614bc).
            try:
                from xmclaw.utils.swallowed_exceptions import record as _swallow
                _swallow("ws_image_intake.save", exc)
            except Exception:  # noqa: BLE001
                pass
            continue

    log.debug("ws.user_frame: user_image_paths=%s", out_paths)
    return out_paths


__all__ = ["save_user_frame_images", "MAX_IMAGES_PER_FRAME", "MAX_IMAGE_BYTES"]
