"""Wave 26 — MediaAttachment IR + normalize_attachments helper."""
from __future__ import annotations

from xmclaw.core.ir import MediaAttachment, normalize_attachments


# ── MediaAttachment ──────────────────────────────────────────────


def test_to_dict_drops_none_fields() -> None:
    att = MediaAttachment(kind="image", path="/tmp/a.png")
    d = att.to_dict()
    assert d == {"kind": "image", "path": "/tmp/a.png"}


def test_to_dict_preserves_set_fields() -> None:
    att = MediaAttachment(
        kind="video", path="/tmp/v.mp4", mime="video/mp4",
        bytes_size=12345, width=1920, height=1080, duration_s=30.5,
    )
    d = att.to_dict()
    assert d == {
        "kind": "video", "path": "/tmp/v.mp4", "mime": "video/mp4",
        "bytes_size": 12345, "width": 1920, "height": 1080,
        "duration_s": 30.5,
    }


def test_from_dict_round_trip() -> None:
    src = MediaAttachment(
        kind="audio", path="/tmp/a.mp3", mime="audio/mpeg",
        bytes_size=4567, duration_s=12.3,
    )
    decoded = MediaAttachment.from_dict(src.to_dict())
    assert decoded == src


def test_from_dict_rejects_unknown_kind() -> None:
    assert MediaAttachment.from_dict({
        "kind": "document", "path": "/tmp/x.pdf",
    }) is None


def test_from_dict_rejects_empty_path() -> None:
    assert MediaAttachment.from_dict({"kind": "image", "path": ""}) is None
    assert MediaAttachment.from_dict({"kind": "image"}) is None


def test_from_dict_rejects_non_dict() -> None:
    assert MediaAttachment.from_dict("not a dict") is None
    assert MediaAttachment.from_dict(None) is None
    assert MediaAttachment.from_dict([1, 2]) is None


def test_public_url_uses_basename() -> None:
    att = MediaAttachment(kind="image", path="C:\\Users\\foo\\bar\\img.png")
    assert att.public_url() == "/api/v2/media/img.png"


# ── normalize_attachments ────────────────────────────────────────


def test_normalize_handles_canonical_list() -> None:
    raw = {
        "attachments": [
            {"kind": "image", "path": "/a.png"},
            {"kind": "video", "path": "/b.mp4"},
        ],
    }
    out = normalize_attachments(raw)
    assert len(out) == 2
    assert out[0].kind == "image"
    assert out[1].kind == "video"


def test_normalize_promotes_legacy_attach_image() -> None:
    """Old code returns ``metadata={"attach_image": "/path"}``. Bridge
    keeps it working."""
    raw = {"attach_image": "/legacy/img.png"}
    out = normalize_attachments(raw)
    assert len(out) == 1
    assert out[0].kind == "image"
    assert out[0].path == "/legacy/img.png"


def test_normalize_dedupes_legacy_against_canonical() -> None:
    """If a tool emits BOTH attach_image AND attachments with the
    same path, we don't surface it twice."""
    raw = {
        "attach_image": "/x.png",
        "attachments": [
            {"kind": "image", "path": "/x.png", "width": 100, "height": 50},
        ],
    }
    out = normalize_attachments(raw)
    assert len(out) == 1
    # The explicit MediaAttachment wins (has the dimensions).
    assert out[0].width == 100


def test_normalize_filters_malformed_items() -> None:
    raw = {
        "attachments": [
            {"kind": "image", "path": "/ok.png"},
            "string-not-dict",
            {"kind": "unknown", "path": "/bad.x"},
            {"kind": "video"},  # missing path
        ],
    }
    out = normalize_attachments(raw)
    assert len(out) == 1
    assert out[0].path == "/ok.png"


def test_normalize_empty_or_none_returns_empty() -> None:
    assert normalize_attachments(None) == []
    assert normalize_attachments({}) == []
    assert normalize_attachments({"attachments": "not a list"}) == []
    assert normalize_attachments("not a dict") == []
