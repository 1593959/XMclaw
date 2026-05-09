"""StrategyBank — vector-indexed store for distilled strategies.

Sprint 3 #6 (ReasoningBank-style). Wraps a vector-store backend
(structurally compatible with :class:`xmclaw.providers.memory.SqliteVecMemory`,
but accepted as ``Any`` so core/ stays import-direction-clean) plus an
embedder with an ``async embed(list[str]) -> list[list[float]]``
method.

Design
------

* **Embedded text = ``f"{when}\\n\\n{then}"``** — the bank embeds the
  full pattern so retrieval can match either the "when" half (a
  situation lookup) or the "then" half (an action lookup).

* **Layer / tag policy**: every strategy is stored at ``layer="long"``
  with ``metadata={"tag": "strategy", ...}``. ``layer="long"`` keeps
  strategies out of the short/working memory budget (they're
  long-lived recall hints, not session scratchpad). The tag lets
  ``prune_unused`` filter cleanly.

* **Idempotency**: :meth:`put` keys on ``Strategy.id``. Re-inserting
  the same id replaces the row. The vec store's primary-key constraint
  on ``item.id`` does the actual deduplication.

* **Iron Rule #1 defence**: :meth:`put` rejects strategies where
  ``evidence_count < MIN_EVIDENCE_COUNT``. Even if the distiller
  somehow lets one through, the bank refuses it.

* **last_retrieved_at touch**: :meth:`retrieve` updates the field on
  each Strategy it returns, by re-putting an updated copy. This is
  the pruning signal — :meth:`prune_unused` keeps recently-touched
  strategies regardless of age.

The bank is async-only — the underlying memory provider is async.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from xmclaw.core.journal.strategy_types import (
    MIN_EVIDENCE_COUNT,
    Strategy,
)

_log = logging.getLogger(__name__)


_LAYER_LONG = "long"
_TAG_STRATEGY = "strategy"


@dataclass(frozen=True, slots=True)
class _MemoryItemShape:
    """Local structural twin of :class:`xmclaw.providers.memory.MemoryItem`.

    Re-declared here because ``xmclaw/core/`` cannot import from
    ``xmclaw/providers/`` (per ``xmclaw/core/AGENTS.md`` §2 and
    ``scripts/check_import_direction.py``). Field names match
    ``MemoryItem`` exactly so :class:`SqliteVecMemory` (and any other
    structural compatible store) accepts the object as-is via duck
    typing.
    """

    id: str
    layer: str
    text: str
    metadata: dict[str, Any]
    embedding: tuple[float, ...] | None = None
    ts: float = 0.0


def _embed_text(s: Strategy) -> str:
    """Canonical embedding text for a strategy."""
    return f"{s.when_pattern}\n\n{s.then_action}"


def _to_metadata(s: Strategy) -> dict[str, Any]:
    """Serialise Strategy fields into a flat metadata dict.

    The vec store's metadata column is JSON; we keep keys flat / typed
    so :meth:`_from_metadata` can rehydrate without ad-hoc parsing.
    """
    payload = asdict(s)
    # Tuples don't survive JSON; lower into list and re-tuple on read.
    payload["evidence_session_ids"] = list(s.evidence_session_ids)
    payload["tag"] = _TAG_STRATEGY
    return payload


def _from_metadata(meta: dict[str, Any]) -> Strategy | None:
    """Reverse of :func:`_to_metadata`. Returns ``None`` on bad shape."""
    try:
        return Strategy(
            id=str(meta["id"]),
            when_pattern=str(meta["when_pattern"]),
            then_action=str(meta["then_action"]),
            evidence_count=int(meta["evidence_count"]),
            evidence_session_ids=tuple(
                str(x) for x in meta.get("evidence_session_ids", ())
            ),
            confidence=float(meta["confidence"]),
            distilled_at=float(meta["distilled_at"]),
            last_retrieved_at=(
                None
                if meta.get("last_retrieved_at") is None
                else float(meta["last_retrieved_at"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _log.debug("strategy_bank.from_metadata: bad shape (%s)", exc)
        return None


class StrategyBank:
    """Index + retrieve distilled strategies.

    Parameters
    ----------
    vec_store:
        Anything implementing the structural subset of
        :class:`xmclaw.providers.memory.MemoryProvider` we use:
        ``async put(layer, item)``, ``async query(layer, *, embedding,
        k, filters)``, ``async forget(item_id)``. Accepting ``Any``
        keeps core/ free of provider imports.
    embedder:
        Anything with ``async embed(list[str]) -> list[list[float]]``.
        Same rationale — kept structural to dodge import direction.
    """

    def __init__(self, vec_store: Any, embedder: Any) -> None:
        self._store = vec_store
        self._embedder = embedder

    async def put(self, s: Strategy) -> None:
        """Idempotently insert/replace ``s`` in the vec store.

        Iron Rule #1 enforcement: rejects ``s.evidence_count <
        MIN_EVIDENCE_COUNT`` with a logged warning. Returns silently
        on rejection rather than raising — the caller is typically a
        bulk distillation pass that should not be killed by one bad
        row.
        """
        if s.evidence_count < MIN_EVIDENCE_COUNT:
            _log.warning(
                "strategy_bank.put.rejected.low_evidence id=%s count=%d",
                s.id, s.evidence_count,
            )
            return

        text = _embed_text(s)
        embeddings = await self._embedder.embed([text])
        if not embeddings:
            _log.warning("strategy_bank.put.embed_empty id=%s", s.id)
            return
        embedding = tuple(embeddings[0])

        # Build a structural twin of MemoryItem locally — see
        # ``_MemoryItemShape`` for why we don't import the real one.
        item = _MemoryItemShape(
            id=s.id,
            layer=_LAYER_LONG,
            text=text,
            metadata=_to_metadata(s),
            embedding=embedding,
            ts=time.time(),
        )
        await self._store.put(_LAYER_LONG, item)

    async def retrieve(
        self, query_text: str, limit: int = 3,
    ) -> list[Strategy]:
        """Top-K cosine search; touches ``last_retrieved_at`` on hits.

        ``query_text`` is embedded once and used as the search vector.
        Results are filtered to ``tag=="strategy"`` so a shared vec
        store with other content (file_chunks etc.) does not leak in.
        On each hit we re-put an updated Strategy copy with
        ``last_retrieved_at = now``; this is what :meth:`prune_unused`
        keys on.
        """
        if limit <= 0:
            return []
        embeddings = await self._embedder.embed([query_text])
        if not embeddings:
            return []
        embedding = list(embeddings[0])

        items = await self._store.query(
            _LAYER_LONG,
            embedding=embedding,
            k=int(limit),
            filters={"tag": _TAG_STRATEGY},
        )

        now = time.time()
        out: list[Strategy] = []
        for it in items:
            s = _from_metadata(it.metadata)
            if s is None:
                continue
            touched = replace(s, last_retrieved_at=now)
            # Re-put through the regular code path — re-embeds the
            # text (cheap one-string call) and bumps the metadata.
            await self.put(touched)
            out.append(touched)
        return out

    async def prune_unused(self, max_age_s: float) -> int:
        """Drop strategies older than ``max_age_s`` and never retrieved.

        "Never retrieved" means ``last_retrieved_at is None``. Anything
        with a recent retrieval timestamp is kept regardless of age —
        the whole point of last_retrieved_at is to say "this strategy
        is still earning its keep".

        Returns the number of strategies removed.
        """
        cutoff = time.time() - float(max_age_s)
        # We piggy-back on the vec store's keyword query to enumerate
        # strategy rows. ``embedding=None`` falls back to non-vector
        # filtering in SqliteVecMemory; a fake store can implement it
        # similarly. ``k`` is large because pruning is rare and we
        # want to inspect the full population.
        items = await self._store.query(
            _LAYER_LONG,
            embedding=None,
            k=10_000,
            filters={"tag": _TAG_STRATEGY},
        )
        removed = 0
        for it in items:
            s = _from_metadata(it.metadata)
            if s is None:
                # Garbage row — drop it too so prune is the cleanup
                # path for malformed metadata.
                await self._store.forget(it.id)
                removed += 1
                continue
            if s.last_retrieved_at is not None:
                # Retrieved-recently strategies are always kept.
                continue
            if s.distilled_at <= cutoff:
                await self._store.forget(s.id)
                removed += 1
        return removed


__all__ = ["StrategyBank"]
