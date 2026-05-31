"""Skill semantic discovery index — embedding-based skill retrieval.

THE fix for "agent can't autonomously use skills with a Chinese query".
The B-238 token-overlap prefilter (``prefilter.py``) admits in its own
docstring that it "DROPS to zero on CJK queries against English skill
descriptions" — so for a Chinese user, the relevant ``skill_<id>`` tool
never even appears in the agent's tool list that turn, and it can't
autonomously call a skill it can't see.

This module supplies the missing **language-agnostic** signal: embed
every skill's description once (cached + batched via
``EmbeddingService.embed_batch``), embed the user query each turn, and
rank by cosine similarity. The prefilter then fuses this with the cheap
token score (``select_relevant_skills(semantic_scores=...)``) so a skill
with zero literal token overlap but high semantic similarity still
survives to the shortlist.

Design choices (research-backed — see
``docs/audit/SKILL_SYSTEM_SOTA_RESEARCH_2026.md`` §⑫, RAG-of-tools /
semantic routing: >100 tools are "unusable" without semantic selection;
RAG-MCP reports ~3.2× selection accuracy + ~99% token reduction):

* **Description-embedding, not name.** The description carries the
  intent; names are terse ids.
* **Per-skill embedding cached by (name, description).** Only NEW or
  CHANGED descriptions hit the provider — steady-state per-turn cost is
  one query embed + an in-memory cosine sweep.
* **Floor gate.** Cosine is rarely 0 for any text pair, so we only
  return scores ``>= floor`` (default 0.30). Below the floor a skill is
  "not semantically relevant" and contributes nothing — this keeps the
  prefilter's ``score > 0`` admission meaningful instead of letting
  every skill leak through on embedding noise.
* **Never raises.** Any embedder error → empty scores → the prefilter
  silently falls back to pure token overlap (today's behaviour). This
  is strictly additive and safe to enable by default.
"""
from __future__ import annotations

from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

#: cosine below this ⇒ "not relevant", omitted from the score map.
DEFAULT_SEMANTIC_FLOOR = 0.30


def _cosine(a: tuple[float, ...], b: tuple[float, ...], a_norm: float) -> float:
    """Cosine similarity given a pre-computed norm for ``a`` (the query —
    reused across all skills). Returns 0.0 on degenerate input."""
    if not a or not b or a_norm <= 0:
        return 0.0
    dot = 0.0
    bn = 0.0
    for x, y in zip(a, b):
        dot += x * y
        bn += y * y
    if bn <= 0:
        return 0.0
    return dot / (a_norm * (bn ** 0.5))


class SkillSemanticIndex:
    """Embedding index over skill descriptions. Reuses a shared
    ``EmbeddingService`` (the one the memory system already wires)."""

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        # name -> (description, embedding). Survives across turns; only
        # rebuilt for skills whose description changed.
        self._vecs: dict[str, tuple[str, tuple[float, ...]]] = {}

    async def _ensure(self, specs: list[Any]) -> None:
        """Embed any skill descriptions not yet cached (or changed).
        Batched in a single provider call; cache hits are free."""
        todo_names: list[str] = []
        todo_descs: list[str] = []
        live: set[str] = set()
        for spec in specs:
            name = getattr(spec, "name", "") or ""
            desc = (getattr(spec, "description", "") or "").strip()
            if not name or not desc:
                continue
            live.add(name)
            cached = self._vecs.get(name)
            if cached is None or cached[0] != desc:
                todo_names.append(name)
                todo_descs.append(desc)
        # Prune embeddings for skills no longer present (uninstalled).
        for stale in [n for n in self._vecs if n not in live]:
            self._vecs.pop(stale, None)
        if not todo_descs:
            return
        vecs = await self._embedder.embed_batch(todo_descs)
        for n, d, v in zip(todo_names, todo_descs, vecs):
            self._vecs[n] = (d, tuple(v))

    async def scores(
        self,
        query: str,
        specs: list[Any],
        *,
        floor: float = DEFAULT_SEMANTIC_FLOOR,
    ) -> dict[str, float]:
        """Return ``{skill_name: cosine}`` for skills with cosine ≥
        ``floor`` against ``query``. Empty dict when there's no embedder,
        no query, or nothing clears the floor. Never raises."""
        if not query or not query.strip():
            return {}
        try:
            await self._ensure(specs)
            qvec = tuple(await self._embedder.embed(query))
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            _log.debug("skill_semantic_index.embed_failed err=%s", exc)
            return {}
        qn = sum(x * x for x in qvec) ** 0.5
        if qn <= 0:
            return {}
        live_names = {
            (getattr(s, "name", "") or "") for s in specs
        }
        out: dict[str, float] = {}
        for name, (_desc, vec) in self._vecs.items():
            if name not in live_names:
                continue
            c = _cosine(qvec, vec, qn)
            if c >= floor:
                out[name] = c
        return out


__all__ = ["SkillSemanticIndex", "DEFAULT_SEMANTIC_FLOOR"]
