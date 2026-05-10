"""Unified-id minter + atomic-write error type — ``xmclaw-architecture-redesign.md`` §3.3.4.

Memory consistency rule (recap): every memory entry has a globally
unique id; vector / graph / temporal indices all use the SAME id when
they refer to the same logical entry. Atomic writes across the three
indices keep them in sync; if any index fails, the others are rolled
back (best-effort compensation — SQLite cross-DB is not transactional).

This module owns:

* ``mint_unified_id(text, ts)`` — short, stable-shape, collision-
  resistant id derivation. Used by ``UnifiedMemorySystem.put`` so
  every fan-out write stamps the same id into every index.
* ``UnifiedWriteError`` — raised when fan-out fails partway through.
  Carries a list of which indices got rolled back successfully so the
  caller can decide whether to surface to the user / retry.

Stdlib-only by design — pulling in ``ulid``/``uuid7``/``ksuid`` for a
24-char hex string would be deps churn for ~zero gain.
"""
from __future__ import annotations

import hashlib
import time
import uuid


def mint_unified_id(text: str, ts: float | None = None) -> str:
    """Return a 24-char hex id derived from ``(text, ts, uuid4)``.

    Shape: 24 lowercase hex chars (96 bits) — enough entropy that
    collision probability across a single XMclaw install is
    cryptographically negligible (≈ 1 in 2⁴⁸ at 16M entries — and we
    expect ≪1M in practice).

    Why 24 chars (not 32 / not 64): URLs / log lines / debug dumps
    fit better, while still vastly exceeding the collision budget.
    Same length as MongoDB ObjectId, which has battle-tested this
    trade-off at scale.

    Determinism: the ``uuid4`` component guarantees uniqueness even
    when called twice with the same ``(text, ts)`` (e.g. two distinct
    facts that happen to be word-for-word identical and minted at the
    same Unix-second resolution). Calling with a fixed ``ts`` does
    NOT make the id deterministic — that's by design; callers who
    want content-deduplication should hash separately and look up the
    existing id.

    Args:
        text: the memory entry's content. Hashed in.
        ts: optional unix timestamp; defaults to ``time.time()``.

    Returns:
        24-char lowercase hex string.
    """
    if ts is None:
        ts = time.time()
    payload = f"{text}|{ts}|{uuid.uuid4().hex}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return digest[:24]


class UnifiedWriteError(Exception):
    """Raised when ``UnifiedMemorySystem.put`` fan-out fails partway.

    Atomic-write contract is best-effort: SQLite cross-DB writes are
    not transactional, so if write #2 fails after write #1 succeeded,
    we attempt to compensate (delete from #1). Compensation can ALSO
    fail (locked DB / disk full mid-rollback). This exception carries:

    * ``indices_written``  — list of index names that committed before
      the failure (e.g. ``["graph", "vec"]``).
    * ``compensated``      — sublist of ``indices_written`` that we
      successfully rolled back. Anything in ``indices_written`` and
      NOT in ``compensated`` is now in an inconsistent state and
      needs manual cleanup or operator attention.
    * ``original``         — the underlying exception that triggered
      the rollback, kept around for ``__cause__`` / debugging.

    The caller sees a clear failure (commit was NOT atomic) plus
    enough provenance to decide whether to retry the same operation,
    surface to the user, or schedule a janitor sweep.
    """

    def __init__(
        self,
        message: str,
        *,
        indices_written: list[str] | None = None,
        compensated: list[str] | None = None,
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.indices_written: list[str] = list(indices_written or [])
        self.compensated: list[str] = list(compensated or [])
        self.original: Exception | None = original

    def __repr__(self) -> str:  # pragma: no cover — debug helper
        return (
            f"UnifiedWriteError({self.args[0]!r}, "
            f"written={self.indices_written}, "
            f"compensated={self.compensated}, "
            f"original={self.original!r})"
        )
