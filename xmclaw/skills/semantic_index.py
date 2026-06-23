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

    def has_pending(self, specs: list[Any]) -> bool:
        """Cheap sync check: is any skill description not yet embedded
        (or changed since)? Lets the caller schedule :meth:`warm` in the
        BACKGROUND only when there's actually work, so a per-turn call is
        free once the index is hot."""
        for spec in specs:
            name = getattr(spec, "name", "") or ""
            desc = (getattr(spec, "description", "") or "").strip()
            if not name or not desc:
                continue
            cached = self._vecs.get(name)
            if cached is None or cached[0] != desc:
                return True
        return False

    async def _embed_many(self, texts: list[str]) -> list[Any]:
        """统一不同 embedder 接口（镜像漂移修复 2026-06-23）。本类原按
        ``EmbeddingService`` 写（``embed(str)->vec`` + ``embed_batch(list)->
        list[vec]``），但实际注入的常是 ``OpenAIEmbeddingProvider`` —— 它的
        ``embed`` 本身就是**批量**接口（``embed(list[str])->list[vec]``），
        传单字符串会被当字符迭代。两处差异曾导致：warm 调 embed_batch →
        AttributeError、scores 调 embed(单串) → 嵌套向量 TypeError → 语义
        索引全空 → 中文 query 对英文技能零浮现 → 安装的技能一个都调不动。
        统一：有 embed_batch 用它，否则把 list 交给 embed（批量）。
        """
        eb = getattr(self._embedder, "embed_batch", None)
        if callable(eb):
            return list(await eb(texts))
        return list(await self._embedder.embed(texts))

    async def warm(self, specs: list[Any]) -> None:
        """Embed any skill descriptions not yet cached (or changed).
        Batched in a single provider call; cache hits are free. Intended
        to run in the BACKGROUND (fire-and-forget) so the per-turn hot
        path never blocks on embedding ~hundreds of descriptions — the
        FIRST skill turn scores token-only (cache cold), and semantic
        recall kicks in a beat later once this completes. Never raises."""
        try:
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
            vecs = await self._embed_many(todo_descs)
            for n, d, v in zip(todo_names, todo_descs, vecs):
                self._vecs[n] = (d, tuple(v))
        except Exception as exc:  # noqa: BLE001 — warming is best-effort
            _log.debug("skill_semantic_index.warm_failed err=%s", exc)

    async def scores(
        self,
        query: str,
        specs: list[Any],
        *,
        floor: float = DEFAULT_SEMANTIC_FLOOR,
    ) -> dict[str, float]:
        """Return ``{skill_name: cosine}`` for skills with cosine ≥
        ``floor`` against ``query``, scored over the ALREADY-WARMED
        cache (call :meth:`warm` to populate it). Empty dict when the
        cache is cold, there's no embedder, no query, or nothing clears
        the floor. Never raises — discovery is strictly best-effort."""
        if not query or not query.strip():
            return {}
        # Cold cache → no query embed (save the call); token-only this
        # turn. warm() populates it for the next turn.
        if not self._vecs:
            return {}
        try:
            qvec = tuple((await self._embed_many([query]))[0])
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
