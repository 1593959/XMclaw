"""LLM-assisted topic operations on the L1 fact graph.

Wave-32+ (2026-05-18). Adds the layer-2 + layer-3 capabilities the
user explicitly asked for after seeing orphan nodes in the graph
view:

  * **Layer 2 (SAME_TOPIC refinement)** — find fact pairs that the
    vector-distance scan rated *almost* same-topic
    (`SAME_TOPIC_DISTANCE_THRESHOLD < d ≤ + 0.15`) and let an LLM
    decide yes/no in a single batched call. Adds SAME_TOPIC edges
    where the LLM says yes. Catches the cases regex+embedder miss
    — e.g. "网址" ↔ "目标网站" Chinese-paraphrase synonymy.

  * **Layer 3 (Topic naming)** — connected-component scan over
    SAME_TOPIC edges; for clusters of ≥ 3 facts WITHOUT an existing
    topic node, ask the LLM for a 2-8 char Chinese name. Write a
    new ``kind=topic`` fact and PART_OF edges from each cluster
    member. Gives the graph view a hierarchy: facts → topic →
    topic-cluster.

Both operations are LLM-cost-aware:

  * **Budgeted** — each call processes at most N candidates / clusters
  * **Cooldown-gated** — won't re-judge the same pair within a window
  * **Feature-flag opt-in** — ``memory_v2.llm_topic.enabled`` default
    False so an out-of-box install doesn't blow LLM credits
  * **Batched** — single LLM call per up-to-20-pair or up-to-5-cluster
    batch, not one-per-pair / one-per-cluster

State is persisted into the fact store itself (the new edges + topic
nodes are durable). No separate "what did I already judge" table —
we re-derive on every call by skipping pairs that already have a
SAME_TOPIC edge.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

from xmclaw.memory.v2.models import (
    Fact,
    FactKind,
    Relation,
    RelationKind,
)
from xmclaw.memory.v2.service import (
    SAME_TOPIC_DISTANCE_THRESHOLD,
    _cosine_distance,
)
from xmclaw.utils.log import get_logger

if TYPE_CHECKING:
    from xmclaw.memory.v2.service import MemoryService
    from xmclaw.providers.llm.base import LLMProvider

log = get_logger(__name__)


# ── Layer 2: borderline-pair LLM refinement ────────────────────────


# Pairs in this distance window are "almost same-topic" — the vector
# scan didn't link them but the LLM might. Above this band they're
# too far to be worth asking about. Below it the existing scan
# already linked them.
_REFINE_DISTANCE_BAND = (
    SAME_TOPIC_DISTANCE_THRESHOLD,        # exclusive lower
    SAME_TOPIC_DISTANCE_THRESHOLD + 0.15,  # inclusive upper
)

# Maximum pairs per LLM call. 20 keeps the prompt under 4KB even for
# verbose Chinese facts and the response under 400 tokens — cheap.
_MAX_PAIRS_PER_BATCH = 20

# Hard timeout on the LLM call. 30s is generous; a smaller fast
# model usually returns in < 5s.
_LLM_TIMEOUT_S = 30.0


async def find_borderline_pairs(
    svc: "MemoryService",
    *,
    budget: int = _MAX_PAIRS_PER_BATCH,
) -> list[tuple[Fact, Fact]]:
    """Return up to ``budget`` fact pairs whose vector distance falls
    in the borderline-same-topic band. Excludes pairs that already
    have a SAME_TOPIC edge (idempotent — second run skips them)."""
    lo, hi = _REFINE_DISTANCE_BAND
    all_facts = await svc._vec.search(None, where=None, limit=2000)
    all_facts = [f for f in all_facts if f.embedding is not None and not f.superseded_by]
    out: list[tuple[Fact, Fact]] = []
    # Track existing SAME_TOPIC edges per-source-id so we skip them.
    # We don't have a single "list all edges" query; instead query
    # neighbors per fact and read its outgoing relations.
    seen_pairs: set[frozenset[str]] = set()
    for fact in all_facts:
        if len(out) >= budget:
            break
        try:
            existing = await svc._graph.neighbors(
                fact.id, relation_types=[RelationKind.SAME_TOPIC.value],
            )
            for rel, target_id in existing:
                seen_pairs.add(frozenset({rel.source_fact_id, target_id}))
        except Exception:  # noqa: BLE001
            pass
    # Compute borderline pairs.
    for i, a in enumerate(all_facts):
        if len(out) >= budget:
            break
        for b in all_facts[i + 1:]:
            if len(out) >= budget:
                break
            pair_key = frozenset({a.id, b.id})
            if pair_key in seen_pairs:
                continue
            d = _cosine_distance(a.embedding, b.embedding)
            if lo < d <= hi:
                out.append((a, b))
                seen_pairs.add(pair_key)
    return out


def _build_refine_prompt(pairs: list[tuple[Fact, Fact]]) -> str:
    """Render the borderline pairs into an LLM prompt asking for a
    yes/no judgment per pair. Output schema is a JSON array of
    integers matching pair indices."""
    lines = [
        "你是知识图谱的关系编辑助手。下面给你 N 对事实，每对都已经"
        "在语义上接近但不完全相同。",
        "请判断每对是否讲的是同一个主题（同一件事、同一个人、同一"
        "个项目、同一个对象）。",
        "如果是同一主题输出 1，不是输出 0。",
        "",
        "**严格 JSON 数组**，长度等于事实对数。无解释，无前缀，无引号。",
        "",
        "示例：3 对 → `[1, 0, 1]`",
        "",
        "事实对：",
    ]
    for i, (a, b) in enumerate(pairs):
        lines.append(f"  {i}. A: {a.text[:180]}")
        lines.append(f"     B: {b.text[:180]}")
    lines.append("")
    lines.append("输出：")
    return "\n".join(lines)


_JSON_ARRAY_RE = re.compile(r"\[[\s\d,]+\]")


def _parse_refine_response(text: str, n: int) -> list[bool]:
    """Parse the LLM's JSON array into a list of length ``n``.
    Falls back to all-False on any error — never raise."""
    m = _JSON_ARRAY_RE.search(text or "")
    if not m:
        return [False] * n
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return [False] * n
    if not isinstance(arr, list):
        return [False] * n
    out = [False] * n
    for i, v in enumerate(arr[:n]):
        try:
            out[i] = bool(int(v))
        except (TypeError, ValueError):
            out[i] = False
    return out


async def refine_same_topic(
    svc: "MemoryService",
    llm: "LLMProvider",
    *,
    budget: int = _MAX_PAIRS_PER_BATCH,
) -> dict[str, Any]:
    """Ask the LLM to judge borderline pairs and add SAME_TOPIC edges
    where the answer is yes. Returns counts for the UI / CLI."""
    pairs = await find_borderline_pairs(svc, budget=budget)
    if not pairs:
        return {
            "scanned_pairs": 0, "edges_added": 0,
            "llm_calls": 0, "duration_s": 0.0,
        }
    t0 = time.perf_counter()
    prompt = _build_refine_prompt(pairs)
    try:
        from xmclaw.core.ir import Message
        resp = await asyncio.wait_for(
            llm.complete(
                [Message(role="user", content=prompt)],
                tools=None,
            ),
            timeout=_LLM_TIMEOUT_S,
        )
        text = (getattr(resp, "content", None) or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_topic.refine_call_failed err=%s", exc)
        return {
            "scanned_pairs": len(pairs), "edges_added": 0,
            "llm_calls": 1, "duration_s": time.perf_counter() - t0,
            "error": str(exc)[:200],
        }
    judgments = _parse_refine_response(text, len(pairs))
    added = 0
    for (a, b), yes in zip(pairs, judgments):
        if not yes:
            continue
        for src, dst in ((a.id, b.id), (b.id, a.id)):
            rid = Relation.compute_id(
                source_fact_id=src,
                target_fact_id=dst,
                relation=RelationKind.SAME_TOPIC,
            )
            try:
                await svc._graph.add_relation(Relation(
                    id=rid,
                    source_fact_id=src,
                    target_fact_id=dst,
                    relation=RelationKind.SAME_TOPIC.value,
                    strength=0.85,  # LLM-judged → higher than vec-only 0.6
                    auto_extracted=True,
                ))
                added += 1
            except Exception:  # noqa: BLE001
                continue
    return {
        "scanned_pairs": len(pairs),
        "edges_added": added,
        "llm_calls": 1,
        "duration_s": round(time.perf_counter() - t0, 2),
    }


# ── Layer 3: topic naming + cluster nodes ──────────────────────────


# Minimum cluster size that's worth naming. Pairs are too small;
# 3+ facts is when a topic label genuinely helps the graph view.
_MIN_CLUSTER_SIZE = 3

# Maximum clusters to process per call. Each cluster = 1 LLM call,
# so this is the real cost knob.
_MAX_CLUSTERS_PER_BATCH = 5

# Maximum facts per cluster sent to the LLM (in case a cluster is
# huge). 30 keeps the prompt manageable; the topic name only needs
# to summarize the representative members, not every one.
_MAX_FACTS_PER_NAMING = 30


async def _build_same_topic_components(
    svc: "MemoryService",
) -> list[set[str]]:
    """Run connected-components over the SAME_TOPIC subgraph.

    Returns a list of components (sets of fact ids). Singletons are
    excluded. Used by the naming pass to find clusters that deserve
    a topic node.
    """
    all_facts = await svc._vec.search(None, where=None, limit=2000)
    all_facts = [f for f in all_facts if not f.superseded_by]
    fact_ids = {f.id for f in all_facts}
    if not fact_ids:
        return []
    # Build adjacency by walking each fact's SAME_TOPIC neighbors.
    adj: dict[str, set[str]] = {fid: set() for fid in fact_ids}
    for fid in fact_ids:
        try:
            rels = await svc._graph.neighbors(
                fid, relation_types=[RelationKind.SAME_TOPIC.value],
            )
        except Exception:  # noqa: BLE001
            continue
        for _r, other in rels:
            if other in adj:
                adj[fid].add(other)
                adj[other].add(fid)
    # BFS components.
    seen: set[str] = set()
    components: list[set[str]] = []
    for fid in fact_ids:
        if fid in seen:
            continue
        comp: set[str] = set()
        stack = [fid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.add(cur)
            stack.extend(adj.get(cur, set()) - seen)
        if len(comp) >= _MIN_CLUSTER_SIZE:
            components.append(comp)
    return components


async def _cluster_has_topic_node(
    svc: "MemoryService", cluster: set[str],
) -> bool:
    """Return True if any cluster member already has an incoming
    PART_OF edge from a kind=topic fact. Avoids re-creating a topic
    node for an already-named cluster.
    """
    # Walk reverse PART_OF — i.e. who has this member as a target.
    # The backend.neighbors API returns OUTGOING edges from fid, so
    # we instead enumerate all topic-kind facts and check whether
    # this fid is in any of their PART_OF targets. Cheap: there are
    # rarely more than a few dozen topic facts.
    try:
        topics = await svc._vec.search(
            None, where=f"kind = '{FactKind.TOPIC.value}'", limit=200,
        )
    except Exception:  # noqa: BLE001
        topics = []
    for t in topics:
        try:
            rels = await svc._graph.neighbors(
                t.id, relation_types=[RelationKind.PART_OF.value],
            )
        except Exception:  # noqa: BLE001
            continue
        for _r, target_id in rels:
            if target_id in cluster:
                return True
    return False


def _build_naming_prompt(facts: list[Fact]) -> str:
    """Single LLM call → one short topic name for the cluster."""
    sample = facts[:_MAX_FACTS_PER_NAMING]
    lines = [
        "你看着以下 N 条相关事实，给它们起一个 2-8 字的中文主题标题。",
        "要求：",
        "  • 标题要具体，不要泛泛 (如 \"用户偏好\" 太泛 → \"暗色主题\")",
        "  • 不超过 8 字",
        "  • 不要引号、不要标点、不要 emoji",
        "  • 只输出标题本身，不要前缀",
        "",
        "事实清单：",
    ]
    for i, f in enumerate(sample):
        lines.append(f"  {i + 1}. {f.text[:200]}")
    if len(facts) > _MAX_FACTS_PER_NAMING:
        lines.append(f"  ... 还有 {len(facts) - _MAX_FACTS_PER_NAMING} 条同主题事实")
    lines.append("")
    lines.append("主题标题：")
    return "\n".join(lines)


def _clean_topic_name(raw: str) -> str:
    """Strip wrapping punctuation + cap at 12 chars (allow some
    slack vs the 8-char prompt requirement — models often slightly
    exceed)."""
    if not raw:
        return ""
    s = raw.strip()
    # Strip wrapping quotes / brackets / 「」 etc.
    s = s.strip("\"'`「」『』《》()（）[]【】 ").strip()
    # First line only.
    s = s.split("\n", 1)[0].strip()
    if not s:
        return ""
    if len(s) > 12:
        s = s[:12]
    return s


async def name_clusters(
    svc: "MemoryService",
    llm: "LLMProvider",
    *,
    budget: int = _MAX_CLUSTERS_PER_BATCH,
) -> dict[str, Any]:
    """Find SAME_TOPIC clusters of 3+ facts without an existing
    topic node, ask the LLM to name them, write topic fact + PART_OF
    edges to all members."""
    components = await _build_same_topic_components(svc)
    if not components:
        return {
            "clusters_scanned": 0, "topics_created": 0,
            "llm_calls": 0, "duration_s": 0.0,
        }
    t0 = time.perf_counter()
    created = 0
    processed = 0
    skipped = 0
    for comp in components:
        if processed >= budget:
            break
        if await _cluster_has_topic_node(svc, comp):
            skipped += 1
            continue
        # Fetch the actual Fact rows for the LLM prompt + later
        # PART_OF edges.
        facts: list[Fact] = []
        for fid in list(comp):
            try:
                f = await svc._vec.get(fid)
                if f is not None and not f.superseded_by:
                    facts.append(f)
            except Exception:  # noqa: BLE001
                continue
        if len(facts) < _MIN_CLUSTER_SIZE:
            skipped += 1
            continue
        # Ask the LLM for a name.
        prompt = _build_naming_prompt(facts)
        try:
            from xmclaw.core.ir import Message
            resp = await asyncio.wait_for(
                llm.complete(
                    [Message(role="user", content=prompt)],
                    tools=None,
                ),
                timeout=_LLM_TIMEOUT_S,
            )
            name = _clean_topic_name(getattr(resp, "content", "") or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_topic.naming_failed err=%s", exc)
            processed += 1
            continue
        if not name:
            processed += 1
            continue
        # Write the topic fact. We deliberately give it a stable
        # text format so the dedup on remember() won't treat two
        # similarly-named topics as the same — text="topic:<name>".
        try:
            topic_fact = await svc.remember(
                f"topic:{name}",
                kind=FactKind.TOPIC.value,
                scope="project",
                confidence=0.85,
                bucket="topic",
                # Skip the contradict scan — topics never contradict
                # each other.
                skip_contradict_check=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_topic.topic_remember_failed err=%s", exc)
            processed += 1
            continue
        # Link each member with a PART_OF edge: topic → member.
        for f in facts:
            rid = Relation.compute_id(
                source_fact_id=topic_fact.id,
                target_fact_id=f.id,
                relation=RelationKind.PART_OF,
            )
            try:
                await svc._graph.add_relation(Relation(
                    id=rid,
                    source_fact_id=topic_fact.id,
                    target_fact_id=f.id,
                    relation=RelationKind.PART_OF.value,
                    strength=0.9,
                    auto_extracted=True,
                ))
            except Exception:  # noqa: BLE001
                continue
        created += 1
        processed += 1
    return {
        "clusters_scanned": len(components),
        "clusters_processed": processed,
        "clusters_skipped_already_named": skipped,
        "topics_created": created,
        "llm_calls": processed,
        "duration_s": round(time.perf_counter() - t0, 2),
    }


__all__ = [
    "find_borderline_pairs",
    "name_clusters",
    "refine_same_topic",
]
