"""MediaAttachment IR — Wave 26.

Unified middle representation for media files a tool produced or
wants the LLM / UI to see. Replaces the private ``metadata.attach_image``
string convention with a structured shape that can also describe
videos, audio, and per-attachment metadata (dimensions / duration).

Producer side (tool):

    return ToolResult(
        call_id=call.id, ok=True,
        content=json.dumps({"path": str(out), ...}),
        metadata={
            "attachments": [
                MediaAttachment(
                    kind="image", path=str(out),
                    mime="image/png", bytes_size=size,
                    width=w, height=h,
                ).to_dict(),
            ],
        },
    )

Consumer side (hop_loop / channel adapter):

    for d in result.metadata.get("attachments", []):
        att = MediaAttachment.from_dict(d)
        if att.kind == "image":
            ...

Backwards compatibility: the legacy ``metadata.attach_image: str``
field still works — hop_loop reads both. New code should prefer
``attachments`` so video and audio land cleanly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

MediaKind = Literal["image", "video", "audio"]


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """One media artifact produced by a tool.

    Fields:
      ``kind``       — coarse type. Drives UI renderer + LLM block
                       translator.
      ``path``       — absolute filesystem path on the daemon. Goes
                       through ``/api/v2/media/<basename>`` for browser
                       access (token-auth gated).
      ``mime``       — RFC 6838 mime type for downstream renderers.
                       Optional; UI / LLM can infer from extension.
      ``bytes_size`` — file size in bytes. Used by hop_loop to refuse
                       embedding huge files inline.
      ``width`` / ``height`` — image / video pixel dimensions.
      ``duration_s`` — video / audio duration in seconds.

    All numeric fields are optional — set what the producer can compute
    cheaply, omit the rest. Consumers MUST handle missing fields.
    """

    kind: MediaKind
    path: str
    mime: str | None = None
    bytes_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None fields so the wire payload stays compact.
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, raw: Any) -> "MediaAttachment | None":
        """Best-effort decode. Returns None on shape mismatches so
        consumers can filter cleanly without try/except.
        """
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        path = raw.get("path")
        if kind not in ("image", "video", "audio"):
            return None
        if not isinstance(path, str) or not path:
            return None
        return cls(
            kind=kind,  # type: ignore[arg-type]
            path=path,
            mime=raw.get("mime") if isinstance(raw.get("mime"), str) else None,
            bytes_size=(
                int(raw["bytes_size"])
                if isinstance(raw.get("bytes_size"), (int, float))
                else None
            ),
            width=(
                int(raw["width"])
                if isinstance(raw.get("width"), (int, float)) else None
            ),
            height=(
                int(raw["height"])
                if isinstance(raw.get("height"), (int, float)) else None
            ),
            duration_s=(
                float(raw["duration_s"])
                if isinstance(raw.get("duration_s"), (int, float)) else None
            ),
        )

    def public_url(self) -> str:
        """Resolve to the daemon's media-route URL the browser /
        external clients can fetch. Token auth is appended by the
        caller (chat_reducer / channel adapter) since they own the
        request context."""
        return f"/api/v2/media/{Path(self.path).name}"


def normalize_attachments(raw: Any) -> list[MediaAttachment]:
    """Turn whatever was in ``ToolResult.metadata`` into a clean list
    of ``MediaAttachment``. Handles three shapes:

      1. ``{"attachments": [{...}, ...]}`` — Wave 26 canonical
      2. ``{"attach_image": "/path/to/img"}`` — legacy single-image
      3. None / malformed — returns []

    Consumers (hop_loop, channel adapters) call this once and operate
    on the normalized list, never on the raw metadata dict.
    """
    if not isinstance(raw, dict):
        return []
    out: list[MediaAttachment] = []
    items = raw.get("attachments")
    if isinstance(items, list):
        for item in items:
            att = MediaAttachment.from_dict(item)
            if att is not None:
                out.append(att)
    # Legacy: ``attach_image: "/path"`` (string). Promote to an image
    # attachment so both paths converge on the same downstream code.
    legacy = raw.get("attach_image")
    if isinstance(legacy, str) and legacy:
        # Deduplicate against any explicit attachment with the same path.
        if not any(a.path == legacy for a in out):
            out.append(MediaAttachment(kind="image", path=legacy))
    return out


__all__ = ["MediaAttachment", "MediaKind", "normalize_attachments"]
