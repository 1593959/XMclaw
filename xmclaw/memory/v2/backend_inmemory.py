"""InMemory backends — test stubs for VectorBackend + GraphBackend.

Used by:
    - Unit tests that don't want to pay the LanceDB import cost
    - CI runs that don't install the ``[memory-full]`` extras
    - Local dry-runs of memory_service before wiring up the
      production backends

Semantic parity with LanceDB backends is the goal: every method has
the same observable contract. Distance metric is cosine. Filter
``where`` parser supports a small SQL-ish subset enough for tests
(=, !=, >, <, >=, <=, AND/OR).
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any

from xmclaw.memory.v2.models import Fact, Relation, RelationKind


# ── Tiny SQL-ish filter parser (for tests only) ───────────────────


_BIN_OP_RE = re.compile(
    r"^\s*(\w+)\s*(=|!=|<=|>=|<|>)\s*(.+?)\s*$"
)

# Match: ``col IN ('a', 'b', 'c')`` — case-insensitive on IN.
_IN_OP_RE = re.compile(
    r"^\s*(\w+)\s+IN\s*\(\s*(.+?)\s*\)\s*$",
    re.IGNORECASE,
)
# Split items on commas not inside quotes. For our test usage,
# everything's single-quoted; a simple split is enough.
_IN_ITEM_RE = re.compile(r"\s*,\s*")


def _unquote(s: str) -> Any:
    """Strip 'quotes' or "quotes" or cast to numeric if it looks like one."""
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "'") or (s[0] == s[-1] == '"')):
        return s[1:-1]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _eval_filter(where: str | None, row: dict[str, Any]) -> bool:
    """Evaluate a tiny SQL-ish ``where`` clause against a dict row.

    Supports:
        col = value
        col != value
        col > value (also <, >=, <=)
        ... AND ... (case-insensitive)
        ... OR ...  (case-insensitive)
        parens for grouping (one level deep)

    NOT a SQL parser; raises ValueError on unrecognised input. This
    is fine — tests use simple filters; production uses LanceDB.
    """
    if where is None or not where.strip():
        return True
    expr = where.strip()
    # Strip one level of outer parens.
    if expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()
    # Split on OR first (lowest precedence)…
    or_parts = re.split(r"\s+OR\s+", expr, flags=re.IGNORECASE)
    if len(or_parts) > 1:
        return any(_eval_filter(p, row) for p in or_parts)
    # …then AND.
    and_parts = re.split(r"\s+AND\s+", expr, flags=re.IGNORECASE)
    if len(and_parts) > 1:
        return all(_eval_filter(p, row) for p in and_parts)
    # Leaf: IN clause first (more specific than binary op match).
    m_in = _IN_OP_RE.match(expr)
    if m_in:
        col = m_in.group(1)
        items_raw = _IN_ITEM_RE.split(m_in.group(2))
        items = [_unquote(it) for it in items_raw]
        cell = row.get(col)
        return cell in items
    # Leaf: binary comparison.
    m = _BIN_OP_RE.match(expr)
    if not m:
        raise ValueError(f"unparseable where clause: {where!r}")
    col, op, val = m.group(1), m.group(2), _unquote(m.group(3))
    cell = row.get(col)
    if cell is None:
        return False
    try:
        if op == "=":
            return cell == val
        if op == "!=":
            return cell != val
        if op == ">":
            return cell > val
        if op == "<":
            return cell < val
        if op == ">=":
            return cell >= val
        if op == "<=":
            return cell <= val
    except TypeError:
        # Mixed types — treat as no match.
        return False
    raise ValueError(f"unknown op: {op}")


# ── Distance ─────────────────────────────────────────────────────


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity in [-1, 1]. 1 = identical, 0 = orthogonal."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── InMemoryVectorBackend ─────────────────────────────────────────


class InMemoryVectorBackend:
    """Dict-backed VectorBackend. Insertion-ordered for stable test
    iteration; KNN by cosine similarity on the embedding column."""

    def __init__(self) -> None:
        self._rows: OrderedDict[str, Fact] = OrderedDict()

    async def upsert(self, records: list[Fact]) -> int:
        n = 0
        now = time.time()
        for rec in records:
            existing = self._rows.get(rec.id)
            if existing is None:
                # Fresh insert.
                self._rows[rec.id] = rec
            else:
                # Merge: bump evidence + advance ts + take max confidence.
                existing.evidence_count += rec.evidence_count
                existing.confidence = max(existing.confidence, rec.confidence)
                # Highest-precision values from new record win for
                # the rest, but keep the original embedding if new
                # didn't supply one.
                if rec.embedding is not None:
                    existing.embedding = rec.embedding
                if rec.source_event_id:
                    existing.source_event_id = rec.source_event_id
                existing.text = rec.text or existing.text
                existing.layer = rec.layer
                existing.ts_last = now
            n += 1
        return n

    async def search(
        self,
        query: list[float] | str | None = None,
        *,
        where: str | None = None,
        limit: int = 8,
    ) -> list[Fact]:
        # Step 1: filter rows by where.
        candidates: list[Fact] = []
        for fact in self._rows.values():
            row = {
                "id": fact.id,
                "kind": fact.kind,
                "scope": fact.scope,
                "confidence": fact.confidence,
                "layer": fact.layer,
                "evidence_count": fact.evidence_count,
                "ts_last": fact.ts_last,
                "ts_first": fact.ts_first,
                "text": fact.text,
            }
            if _eval_filter(where, row):
                candidates.append(fact)

        # Step 2: rank.
        if query is None:
            # Pure-filter listing — order by ts_last DESC.
            candidates.sort(key=lambda f: f.ts_last, reverse=True)
            return candidates[:limit]

        if isinstance(query, str):
            # Keyword fallback: contains-match (case-insensitive),
            # then by evidence_count DESC.
            q = query.lower()
            scored = [
                (f.evidence_count if q in f.text.lower() else -1, f)
                for f in candidates
            ]
            scored = [(s, f) for s, f in scored if s >= 0]
            scored.sort(key=lambda sf: sf[0], reverse=True)
            return [f for _, f in scored[:limit]]

        # Vector query: cosine similarity.
        qvec = tuple(query)
        scored_v = []
        for f in candidates:
            if f.embedding is None:
                continue
            sim = _cosine(qvec, f.embedding)
            scored_v.append((sim, f))
        scored_v.sort(key=lambda sf: sf[0], reverse=True)
        return [f for _, f in scored_v[:limit]]

    async def delete(self, where: str) -> int:
        to_drop = [
            fid for fid, fact in self._rows.items()
            if _eval_filter(where, {
                "id": fact.id, "kind": fact.kind, "scope": fact.scope,
                "confidence": fact.confidence, "layer": fact.layer,
                "evidence_count": fact.evidence_count,
                "ts_last": fact.ts_last, "ts_first": fact.ts_first,
                "text": fact.text,
            })
        ]
        for fid in to_drop:
            del self._rows[fid]
        return len(to_drop)

    async def count(self, where: str | None = None) -> int:
        if where is None:
            return len(self._rows)
        n = 0
        for fact in self._rows.values():
            if _eval_filter(where, {
                "id": fact.id, "kind": fact.kind, "scope": fact.scope,
                "confidence": fact.confidence, "layer": fact.layer,
                "evidence_count": fact.evidence_count,
                "ts_last": fact.ts_last, "ts_first": fact.ts_first,
                "text": fact.text,
            }):
                n += 1
        return n

    async def get(self, fact_id: str) -> Fact | None:
        return self._rows.get(fact_id)

    async def close(self) -> None:
        # Nothing to clean up; method present for Protocol parity.
        return None


# ── InMemoryGraphBackend ──────────────────────────────────────────


class InMemoryGraphBackend:
    """Dict-backed GraphBackend. Adjacency list keyed by source id;
    relations stored by id for O(1) upsert."""

    def __init__(self) -> None:
        self._rels: OrderedDict[str, Relation] = OrderedDict()
        self._out: dict[str, list[str]] = {}   # source_fact_id → [rel_id]

    async def add_relation(self, rel: Relation) -> None:
        existing = self._rels.get(rel.id)
        if existing is None:
            self._rels[rel.id] = rel
            self._out.setdefault(rel.source_fact_id, []).append(rel.id)
            return
        # Idempotent: keep most recent strength + ts.
        existing.strength = max(existing.strength, rel.strength)
        existing.ts = max(existing.ts, rel.ts)
        existing.auto_extracted = rel.auto_extracted or existing.auto_extracted

    async def add_relations(self, rels: list[Relation]) -> int:
        for r in rels:
            await self.add_relation(r)
        return len(rels)

    async def remove_relation(self, rel_id: str) -> None:
        rel = self._rels.pop(rel_id, None)
        if rel is None:
            return
        bucket = self._out.get(rel.source_fact_id)
        if bucket:
            bucket[:] = [r for r in bucket if r != rel_id]

    async def neighbors(
        self,
        fact_id: str,
        *,
        relation_types: list[str] | None = None,
        max_hops: int = 1,
    ) -> list[tuple[Relation, str]]:
        seen: set[str] = {fact_id}
        frontier = [fact_id]
        out: list[tuple[Relation, str]] = []
        for _ in range(max(1, max_hops)):
            next_frontier: list[str] = []
            for src in frontier:
                for rel_id in self._out.get(src, ()):
                    rel = self._rels[rel_id]
                    if relation_types and rel.relation not in relation_types:
                        continue
                    out.append((rel, rel.target_fact_id))
                    if rel.target_fact_id not in seen:
                        seen.add(rel.target_fact_id)
                        next_frontier.append(rel.target_fact_id)
            frontier = next_frontier
        return out

    async def find_related(
        self,
        fact_ids: list[str],
        *,
        max_hops: int = 1,
        relation_types: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        nodes: set[str] = set(fact_ids)
        edges: list[Relation] = []
        for fid in fact_ids:
            for rel, target in await self.neighbors(
                fid, relation_types=relation_types, max_hops=max_hops,
            ):
                edges.append(rel)
                nodes.add(target)
        # Sort edges by strength DESC and cap.
        edges.sort(key=lambda r: r.strength, reverse=True)
        edges = edges[:limit]
        # Deduplicate by id (BFS can revisit).
        seen_e: dict[str, Relation] = {e.id: e for e in edges}
        return {
            "nodes": sorted(nodes),
            "edges": list(seen_e.values()),
        }

    async def contradictions_of(self, fact_id: str) -> list[str]:
        out = []
        for rel_id in self._out.get(fact_id, ()):
            rel = self._rels[rel_id]
            if rel.relation == RelationKind.CONTRADICTS.value:
                out.append(rel.target_fact_id)
        return out

    async def close(self) -> None:
        return None


__all__ = [
    "InMemoryGraphBackend",
    "InMemoryVectorBackend",
]
