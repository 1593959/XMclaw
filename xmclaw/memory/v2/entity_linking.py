"""Entity linking layer — Mem0 v3 + HippoRAG-inspired.

Extracts entities from facts and user queries, builds an entity→fact
index for graph-enhanced retrieval. Supports Personalized PageRank
propagation from query entities through the fact graph.

Reference: HippoRAG (arXiv:2405.14831, NeurIPS 2024)
           Mem0 v3 entity linking (mem0.ai, ECAI 2026)
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# Lightweight CJK-aware entity extraction regex. Captures:
#   - English words ≥ 3 chars (proper nouns, technical terms)
#   - CJK bigrams (Chinese compound words)
#   - Numeric entities (IPs, versions, dates)
_ENTITY_RE = re.compile(
    r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)"
    r"|([一-鿿]{2,4})"
    r"|(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
    r"|([A-Z][a-zA-Z0-9_]{2,})"
    r"|([一-鿿][一-鿿A-Za-z0-9_]{1,15}[一-鿿])"
)


class EntityLinker:
    """Maps entities ↔ facts for graph-enhanced retrieval."""

    def __init__(self) -> None:
        self._entity_to_facts: defaultdict[str, set[str]] = defaultdict(set)
        self._fact_to_entities: defaultdict[str, set[str]] = defaultdict(set)

    def index_fact(self, fact_id: str, text: str) -> None:
        entities = set()
        for m in _ENTITY_RE.finditer(text):
            entity = m.group(0).strip().lower()
            if len(entity) >= 2:
                entities.add(entity)
        for e in entities:
            self._entity_to_facts[e].add(fact_id)
        self._fact_to_entities[fact_id] = entities

    def unindex_fact(self, fact_id: str) -> None:
        ents = self._fact_to_entities.pop(fact_id, set())
        for e in ents:
            s = self._entity_to_facts.get(e)
            if s:
                s.discard(fact_id)
                if not s:
                    del self._entity_to_facts[e]

    def extract_query_entities(self, query: str) -> list[str]:
        entities: list[str] = []
        for m in _ENTITY_RE.finditer(query):
            e = m.group(0).strip().lower()
            if len(e) >= 2 and e not in entities:
                entities.append(e)
        return entities

    def seed_facts(self, query_entities: list[str]) -> set[str]:
        """Return all fact IDs linked to any query entity."""
        seeds: set[str] = set()
        for e in query_entities:
            seeds.update(self._entity_to_facts.get(e, set()))
        return seeds or self._all_fact_ids()

    def personalized_pagerank(
        self,
        seeds: set[str],
        neighbors_fn: Any,
        *,
        alpha: float = 0.85,
        max_iters: int = 50,
        tol: float = 1e-6,
    ) -> dict[str, float]:
        """Run Personalized PageRank seeded on query-linked facts.

        HippoRAG (arXiv:2405.14831 §2.2): PPR exploits the graph
        structure to propagate relevance from seed nodes — a fact
        connected to many relevant facts gets a higher score.

        Reference: Jeh & Widom (2003), "Scaling Personalized Web Search"
        """
        if not seeds:
            return {}

        # Build adjacency: for each fact, collect neighbors.
        adj: dict[str, list[str]] = {}
        all_nodes: set[str] = set(seeds)
        for fid in seeds:
            if fid not in adj:
                try:
                    pairs = neighbors_fn(fid, max_hops=1)
                    nbrs = [t for _, t in pairs] if pairs else []
                    adj[fid] = nbrs
                    all_nodes.update(nbrs)
                except Exception:  # noqa: BLE001
                    adj[fid] = []

        # Initialize scores.
        scores = {n: (1.0 / len(seeds) if n in seeds else 0.0) for n in all_nodes}
        for _ in range(max_iters):
            diff = 0.0
            new_scores: dict[str, float] = {}
            for node in all_nodes:
                rank = sum(
                    scores[nbr] / max(1, len(adj.get(nbr, [])))
                    for nbr in adj.get(node, [])
                    if nbr in all_nodes
                )
                new_scores[node] = (1 - alpha) * (1.0 if node in seeds else 0.0) / max(1, len(seeds)) + alpha * rank
                diff += abs(new_scores[node] - scores.get(node, 0.0))
            scores = new_scores
            if diff < tol:
                break

        return scores
