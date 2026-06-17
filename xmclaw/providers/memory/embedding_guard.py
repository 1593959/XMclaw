"""Embedding-space fingerprint guard.

2026-06-17. Switching the embedding model (or its output dimension) makes
previously-stored vectors incompatible — cosine distances across two
embedding spaces are meaningless, so recall silently returns garbage (or
LanceDB hard-errors on a dimension change). This guard persists the
active embedder's :pyattr:`fingerprint` next to the index and detects a
change on the next boot, so the daemon can warn the user to rebuild
instead of degrading silently.

Deliberately tiny + dependency-free: one sidecar file, three states
(``fresh`` / ``match`` / ``mismatch``). It never deletes or rewrites the
index — surfacing the mismatch is the caller's job (log + doctor).
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

_MARKER_NAME = ".embedding_fingerprint"

__all__ = ["fingerprint_status", "guard_embedder"]


def fingerprint_status(
    marker_dir: str | Path, fingerprint: str,
) -> tuple[str, str | None]:
    """Compare ``fingerprint`` against the persisted marker in
    ``marker_dir``.

    Returns ``(state, previous)`` where ``state`` is:

    * ``"fresh"``    — no marker existed; it is now written. ``previous`` None.
    * ``"match"``    — marker equals ``fingerprint``. ``previous`` == it.
    * ``"mismatch"`` — marker differs. ``previous`` is the stored value. The
      marker is NOT overwritten, so the warning persists across boots until
      the index is rebuilt (which should rewrite it).
    """
    d = Path(marker_dir)
    marker = d / _MARKER_NAME
    try:
        prev = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    except OSError:
        prev = None

    if prev is None:
        try:
            d.mkdir(parents=True, exist_ok=True)
            marker.write_text(fingerprint, encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            _log.debug("embedding_guard.write_failed dir=%s err=%s", d, exc)
        return "fresh", None
    if prev == fingerprint:
        return "match", prev
    return "mismatch", prev


def guard_embedder(marker_dir: str | Path, embedder: object) -> bool:
    """Check ``embedder.fingerprint`` against the marker in ``marker_dir``.

    On mismatch, log a prominent warning with remediation guidance and
    return True. Returns False for fresh / match / when the embedder has no
    fingerprint. Never raises — a guard failure must not block boot."""
    try:
        fp = getattr(embedder, "fingerprint", None)
        if not isinstance(fp, str) or not fp:
            return False
        state, prev = fingerprint_status(marker_dir, fp)
        if state == "mismatch":
            _log.warning(
                "embedding_guard.MISMATCH: the embedding model changed "
                "(was %r, now %r). Vectors stored under the old model are "
                "incompatible — semantic recall will be degraded until the "
                "memory index is rebuilt. Rebuild it (re-index your memory) "
                "or revert to the previous embedding model.",
                prev, fp,
            )
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        _log.debug("embedding_guard.check_failed err=%s", exc)
        return False
