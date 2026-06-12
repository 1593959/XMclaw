"""WS user-frame file intake — decode non-image attachments to disk.

Phase 10 (2026-06-12): images already flow through
``ws_image_intake.save_user_frame_images`` into vision blocks. This
sibling handles **everything else** the user drags/pastes into the
composer — documents, code, audio, video, archives.

Design follows the unified-paths philosophy: we don't try to "understand"
the file inline. We save it to the uploads dir (name preserved) and
hand the agent a short note with the on-disk path so it can reach for
its existing tools — ``file_read`` for text/code, ``voice_transcribe``
for audio, ``view_video`` for video, etc. The file landing on disk IS
the integration point; no per-type decoder here.

Frame shape (parallel to ``images``)::

    {"type": "user", "content": "...",
     "files": [{"name": "report.pdf", "mime": "application/pdf",
                "data_url": "data:application/pdf;base64,..."}]}

``save_user_frame_files`` returns the saved descriptors so the WS
handler can both (a) fold a reference note into ``content`` and
(b) surface them on the echoed USER_MESSAGE event for the UI.
"""
from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xmclaw.utils.log import get_logger

log = get_logger(__name__)

MAX_FILES_PER_FRAME = 6
# Generous vs images — a short video / audio clip is the point. The
# data: URL round-trips through base64 (≈ +33%) so the browser side
# should keep raw payload under ~36MB to stay below this.
MAX_FILE_BYTES = 48 * 1024 * 1024

# Mime → extension fallback when the supplied filename has none.
_EXT_FALLBACK = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/zip": ".zip",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
}

# Coarse kind from mime — drives the hint verb the agent sees.
_TEXTUAL_HINT = (
    "可用 file_read 读取它的内容"
)
_KIND_HINT = {
    "audio": "可用 voice_transcribe 转写它",
    "video": "可用 view_video / 截帧工具查看它",
    "image": "可用视觉直接查看它",  # 理论上走 images 通道，留兜底
}


def _safe_name(name: str, mime: str) -> str:
    """Sanitise the client-supplied filename to a bare basename with
    a sensible extension. Never trust the client path."""
    base = Path(str(name or "")).name.strip() or "upload"
    # 去掉可能的目录穿越残留 + 控制字符
    base = "".join(c for c in base if c.isprintable() and c not in '\\/:*?"<>|')
    if not base:
        base = "upload"
    if not Path(base).suffix:
        base += _EXT_FALLBACK.get((mime or "").lower(), ".bin")
    return base


@dataclass(frozen=True, slots=True)
class SavedFile:
    path: str
    name: str
    mime: str
    bytes_size: int
    kind: str  # text | audio | video | other

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "mime": self.mime,
            "bytes_size": self.bytes_size,
            "kind": self.kind,
        }


def _kind_of(mime: str) -> str:
    m = (mime or "").lower()
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    if (
        m.startswith("text/")
        or m in ("application/json", "application/xml")
        or m.endswith("+json")
        or m.endswith("+xml")
    ):
        return "text"
    return "other"


def save_user_frame_files(raw_files: Any, uploads_dir: Path) -> list[SavedFile]:
    """Decode the ``files`` field of a WS user frame to disk.

    Mirrors ``save_user_frame_images``' defensive contract: never
    raises, skips bad/oversized entries with a warn-log, returns the
    list of successfully-saved descriptors. Filenames are preserved
    (sanitised + de-duplicated) so the agent's ``file_read`` path and
    the user's mental model line up.
    """
    if not isinstance(raw_files, list):
        return []

    uploads_dir.mkdir(parents=True, exist_ok=True)
    out: list[SavedFile] = []

    for entry in raw_files[:MAX_FILES_PER_FRAME]:
        if not isinstance(entry, dict):
            continue
        data_url = entry.get("data_url") or entry.get("dataUrl")
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            continue
        try:
            header, payload_b64 = data_url.split(",", 1)
            meta = header[len("data:"):]
            mime = (meta.split(";")[0] or "application/octet-stream").lower()
            raw_bytes = base64.b64decode(payload_b64)
            if len(raw_bytes) > MAX_FILE_BYTES:
                log.warning(
                    "ws.user_frame: reject oversized file: %s bytes (%s)",
                    len(raw_bytes), entry.get("name"),
                )
                continue
            name = _safe_name(str(entry.get("name") or ""), mime)
            dst = uploads_dir / name
            # 同名去重：append 计数器（与 send_media 工具同策略）。
            if dst.exists():
                stem, suffix = Path(name).stem, Path(name).suffix
                dst = uploads_dir / f"{stem}_{int(time.time())}_{uuid.uuid4().hex[:4]}{suffix}"
            dst.write_bytes(raw_bytes)
            out.append(
                SavedFile(
                    path=str(dst),
                    name=dst.name,
                    mime=mime,
                    bytes_size=len(raw_bytes),
                    kind=_kind_of(mime),
                )
            )
            log.debug("ws.user_frame: saved file to %s", dst)
        except Exception as exc:  # noqa: BLE001 — skip bad blobs
            log.warning("ws.user_frame: file save failed: %s", exc)
            try:
                from xmclaw.utils.swallowed_exceptions import record as _swallow
                _swallow("ws_file_intake.save", exc)
            except Exception:  # noqa: BLE001
                pass
            continue

    return out


def build_files_note(files: list[SavedFile]) -> str:
    """Render a compact note appended to the user message so the agent
    knows what landed on disk and which tool reaches it.

    Empty list → empty string (caller appends unconditionally)."""
    if not files:
        return ""
    lines = ["\n\n<user-uploaded-files>"]
    for f in files:
        hint = _KIND_HINT.get(f.kind, _TEXTUAL_HINT if f.kind == "text" else _TEXTUAL_HINT)
        size_kb = max(1, f.bytes_size // 1024)
        lines.append(f"- {f.path} ({f.mime}, {size_kb}KB) — {hint}")
    lines.append("</user-uploaded-files>")
    return "\n".join(lines)


__all__ = [
    "save_user_frame_files",
    "build_files_note",
    "SavedFile",
    "MAX_FILES_PER_FRAME",
    "MAX_FILE_BYTES",
]
