"""Per-turn similarity recall — the dynamic axis of memory v3.

Memory v3 has two complementary recall paths:

1. **Structural axis** (writer-side, stable, cache-friendly):
   facts with a registered ``bucket`` → rendered into ``.md`` files →
   loaded as part of the system prompt verbatim every turn. Always
   on, always the same shape, so prompt caching hits. See
   ``xmclaw.core.persona.v2_renderer``.

2. **Similarity axis** (reader-side, dynamic, this module):
   the user's current message gets embedded; the top-K most
   similar facts from LanceDB are formatted into a ``<recalled>``
   block and **prepended to the user message itself** (not the
   system prompt — so prompt caching stays intact). This surfaces
   facts that don't have a bucket route (or that have one but
   would benefit from being highlighted because the current turn
   is specifically about them).

Why both? The structural axis handles "always-relevant" knowledge
(who the user is, agent's values, fixed project facts). The
similarity axis handles "relevant right now" knowledge (the
specific episode / commitment / lesson that matches what the user
is asking about this turn). Together they cover OpenClaw's
auto-recall and Hermes's frozen-bootstrap injection patterns
without either path interfering with prompt cache.

This module is **pure** — no agent_loop / hop_loop / hook engine
imports. Callers (the agent loop, primarily) pass in the
``MemoryService`` and get back the recalled block string, ready
to prepend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


# ─── Defaults the caller can override ─────────────────────────────


# Facts whose buckets already render into the system prompt's .md
# files would be double-injected if we re-recalled them here. These
# are the buckets that are reliably surfaced via the structural
# axis; the similarity axis skips them to save tokens and avoid
# repeating the agent's persistent identity / values facts every
# turn. Override via ``recall_for_message(exclude_buckets=...)``.
_DEFAULT_EXCLUDE_BUCKETS: frozenset[str] = frozenset({
    "agent_identity",
    "user_identity",
    "user_preference",
    "values",
})


# Defaults — modest k so we don't crowd user message; loose
# similarity floor so a moderately-related fact still surfaces.
_DEFAULT_K = 8
_DEFAULT_MIN_SIMILARITY = 0.65
_DEFAULT_MIN_USER_MESSAGE_CHARS = 4   # 1-3 char turns ("ok", "?") → skip

# 2026-05-29 emergency cap: ``recall`` happens on the user-turn
# critical path BEFORE the LLM call, so it MUST be bounded hard.
#
# Real-world incident (chat-b09a3ad4): without this cap the turn
# spun 6245s (104 minutes) waiting for ``recall_hybrid`` to scan
# 5K LanceDB rows + rebuild a Python BM25 index per query. The
# whole class of failure goes away once recall is async-bounded.
#
# Hermes ([memory-providers](https://github.com/NousResearch/hermes-agent))
# avoids the same pitfall by running recall as a **background
# prefetch** between turns — results are cached before the user's
# next message arrives, so the LLM call never waits on recall.
# OpenClaw's memory-lancedb-hybrid plugin uses LanceDB's native
# FTS index (C++ side) instead of a per-query Python BM25 rebuild,
# so the keyword path is O(log N) instead of O(N).
#
# We don't have either of those yet. Until Phase 5 lands a proper
# background prefetch / native FTS, the timeout is what protects
# the turn. 1s was short enough that even a stalled embedding call
# fails fast; raised to 3s in Wave-1 (2026-06-06) after real-world
# observation that 1s kills legitimate large-store recalls.
_DEFAULT_TIMEOUT_S = 3.0


# ─── Recall ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RecalledFact:
    """One fact surfaced for ``<recalled>`` injection.

    Pared-down shape — caller doesn't need the full Fact / embedding
    blob, just enough to render the inline block.
    """

    fid: str
    text: str
    bucket: str
    kind: str
    ts_first: float
    similarity: float


async def recall_for_message(
    memory_service: Any,
    user_message: str,
    *,
    k: int = _DEFAULT_K,
    min_similarity: float = _DEFAULT_MIN_SIMILARITY,
    exclude_buckets: Sequence[str] | None = None,
    min_user_message_chars: int = _DEFAULT_MIN_USER_MESSAGE_CHARS,
    use_hybrid: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    query_embedding: list[float] | None = None,
    valid_at: float | None = None,   # Wave-4: point-in-time query
) -> list[RecalledFact]:
    """Run a similarity recall against the LanceDB store.

    Returns ``[]`` (and never raises) when:
      - the user message is trivially short (≤ ``min_user_message_chars``)
      - ``memory_service`` is None
      - the underlying ``recall`` call fails or times out
      - no candidate clears ``min_similarity``

    ``use_hybrid`` (default True): when True AND the service exposes
    ``recall_hybrid``, fuse vector + BM25. Default True since Wave-2
    (2026-06-06); the BM25 path is guarded by ``rank_bm25`` availability
    and a 500ms per-call deadline so large stores fall back to pure
    vector automatically.

    ``timeout_s`` (default 3.0s): hard wall-clock cap. This runs on
    the user-turn critical path BEFORE the LLM call — every
    millisecond delays the agent's reply. A timeout returns ``[]``
    just like any other failure; the turn proceeds without a
    ``<recalled>`` block. See the constant docstring above for
    incident context.

    Sorted by descending similarity. ``ts_first`` lets the renderer
    optionally show a date stamp; we keep the field on the dataclass
    even though some viewers may not display it.
    """
    import asyncio as _asyncio

    text = (user_message or "").strip()
    if len(text) < min_user_message_chars:
        return []
    if memory_service is None:
        return []

    excluded = (
        frozenset(exclude_buckets)
        if exclude_buckets is not None
        else _DEFAULT_EXCLUDE_BUCKETS
    )
    # Hybrid path is opt-in via ``use_hybrid`` — default sticks with
    # plain vector recall because the current hybrid implementation
    # rebuilds a Python BM25 index per query (the chat-b09a3ad4
    # incident root cause). Flip on when LanceDB native FTS lands.
    if use_hybrid and hasattr(memory_service, "recall_hybrid"):
        recall_coro = memory_service.recall_hybrid(
            text,
            k=max(k * 2, 16),
            min_confidence=0.0,
            include_superseded=False,
            valid_at=valid_at,   # Wave-4: point-in-time
        )
    else:
        if query_embedding is not None:
            recall_coro = memory_service.recall(
                query_embedding,
                k=max(k * 2, 16),
                min_confidence=0.0,
                include_relations=False,
                include_superseded=False,
                valid_at=valid_at,   # Wave-4: point-in-time
            )
        else:
            recall_coro = memory_service.recall(
                text,
                k=max(k * 2, 16),
                min_confidence=0.0,
                include_relations=False,
                include_superseded=False,
                valid_at=valid_at,   # Wave-4: point-in-time
            )

    try:
        hits = await _asyncio.wait_for(recall_coro, timeout=timeout_s)
    except _asyncio.TimeoutError:
        try:
            from xmclaw.utils.log import get_logger
            get_logger(__name__).info(
                "auto_recall.timeout after=%.1fs (turn proceeds without recall)",
                timeout_s,
            )
            # Wave-1 fix: emit metric for monitoring dashboard
            try:
                from xmclaw.core.bus.events import emit_event
                emit_event("metric", {
                    "name": "recall_timeout_count",
                    "value": 1,
                    "tags": {"timeout_s": str(timeout_s)},
                })
            except Exception:
                pass
        except Exception:  # noqa: BLE001
            pass
        return []
    except Exception:  # noqa: BLE001 — never block the turn on recall
        return []

    out: list[RecalledFact] = []
    for h in hits:
        f = h.fact
        bucket = (getattr(f, "bucket", "") or "").strip()
        if bucket in excluded:
            continue
        # Distance → similarity: most backends report cosine distance
        # so ``similarity = 1 - distance``. Clip into [0, 1].
        try:
            distance = float(h.distance)
        except (TypeError, ValueError):
            distance = 1.0
        similarity = max(0.0, min(1.0, 1.0 - distance))
        if similarity < min_similarity:
            continue
        out.append(RecalledFact(
            fid=(getattr(f, "id", "") or "")[:12],
            text=(getattr(f, "text", "") or "").strip(),
            bucket=bucket or "misc",
            kind=(getattr(f, "kind", "") or "fact"),
            ts_first=float(getattr(f, "ts_first", 0.0) or 0.0),
            similarity=similarity,
        ))
        if len(out) >= k:
            break

    return out


def render_recalled_block(hits: Sequence[RecalledFact]) -> str:
    """Format hits as a ``<recalled>`` XML-ish block for the LLM.

    The block format echoes how Hermes / OpenClaw surface auxiliary
    context — leading tag + one bullet per fact + closing tag. The
    fid in each bullet lets the agent quote-back / forget / replace
    a recalled fact without a separate ``memory_search`` round-trip.

    Returns empty string when ``hits`` is empty so the caller can
    just ``prefix + user_message`` without checking.
    """
    if not hits:
        return ""
    lines = ["<recalled relevance=\"similarity-top-k\">"]
    for h in hits:
        sim = f"{h.similarity:.2f}"
        bucket = h.bucket
        text = h.text.replace("\n", " ")
        suffix_fid = f" [fid:{h.fid}]" if h.fid else ""
        lines.append(
            f"- ({sim} | {bucket}) {text}{suffix_fid}"
        )
    lines.append("</recalled>")
    return "\n".join(lines)


def prepend_recalled_block(user_message: str, hits: Sequence[RecalledFact]) -> str:
    """Convenience: format + prepend in one call.

    Pre-2026-05-28: agent_loop only had the structural axis. Long
    sessions saw the agent "fail to remember" facts that lived in
    LanceDB but had no bucket route. This function closes that gap
    every turn by ensuring relevant facts ride on the user message
    itself, regardless of whether they made it into a .md file.
    """
    block = render_recalled_block(hits)
    if not block:
        return user_message
    return f"{block}\n\n{user_message}"


__all__ = [
    "RecalledFact",
    "recall_for_message",
    "render_recalled_block",
    "prepend_recalled_block",
]
