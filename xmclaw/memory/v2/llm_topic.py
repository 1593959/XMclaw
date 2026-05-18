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

# Hard timeout on the LLM call. Tightened 30 → 15s so the browser
# fetch doesn't time out before the daemon does. If the LLM is too
# slow for 15s, treat it as a failure and skip.
_LLM_TIMEOUT_S = 15.0


async def find_borderline_pairs(
    svc: "MemoryService",
    *,
    budget: int = _MAX_PAIRS_PER_BATCH,
) -> list[tuple[Fact, Fact]]:
    """Return up to ``budget`` fact pairs whose vector distance falls
    in the borderline-same-topic band. Excludes pairs that already
    have a SAME_TOPIC edge (idempotent — second run skips them).

    Wave-32+ perf fix: the pre-fix path ran a graph-neighbors query
    on EVERY fact up front to seed ``seen_pairs`` — for a 200-fact
    store that's 200 sequential DB round-trips before the LLM call,
    which routinely blew past the browser's fetch timeout and
    produced "Failed to fetch" in the UI. Restructured to:
      1. Find candidate pairs in the cheap O(N²) cosine pass first
      2. Only THEN look up existing edges, ONLY for the candidates
    Bounded work — for budget=20 we do at most 40 neighbor queries
    instead of N. Plus capped the initial scan at 300 facts (limit
    was 2000) — beyond that the graph is too big for one-shot LLM
    refinement anyway.
    """
    lo, hi = _REFINE_DISTANCE_BAND
    all_facts = await svc._vec.search(None, where=None, limit=300)
    all_facts = [f for f in all_facts if f.embedding is not None and not f.superseded_by]
    # Step 1: cheap O(N²) cosine pass to find candidates. Over-collect
    # 3× the budget so we have slack after filtering already-linked
    # pairs in step 2.
    candidates: list[tuple[Fact, Fact]] = []
    over_budget = budget * 3
    for i, a in enumerate(all_facts):
        if len(candidates) >= over_budget:
            break
        for b in all_facts[i + 1:]:
            if len(candidates) >= over_budget:
                break
            d = _cosine_distance(a.embedding, b.embedding)
            if lo < d <= hi:
                candidates.append((a, b))
    # Step 2: filter out pairs that already have a SAME_TOPIC edge.
    # Touch the graph ONLY for the candidate facts (not all facts).
    seen_pairs: set[frozenset[str]] = set()
    touched_ids: set[str] = set()
    for a, b in candidates:
        for fid in (a.id, b.id):
            if fid in touched_ids:
                continue
            touched_ids.add(fid)
            try:
                existing = await svc._graph.neighbors(
                    fid, relation_types=[RelationKind.SAME_TOPIC.value],
                )
                for rel, target_id in existing:
                    seen_pairs.add(
                        frozenset({rel.source_fact_id, target_id}),
                    )
            except Exception:  # noqa: BLE001
                pass
    out: list[tuple[Fact, Fact]] = []
    for a, b in candidates:
        if len(out) >= budget:
            break
        pair_key = frozenset({a.id, b.id})
        if pair_key in seen_pairs:
            continue
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


def _entity_tier(ent_type: str) -> int:
    """Tier ranking for entity distinctiveness. Higher tier = more
    distinctive = better cluster anchor.

      url      4   — full URL incl. path, near-unique
      domain   3   — domain only, still mostly unique
      ascii_id 2   — admin / pw310 / kebab-case ids
      cjk_bigram 1 — "陪玩" / "网址" / "账号" — common, weakest

    Unknown types fall to 0. Used by entity-anchored clustering to
    pick a fact's PRIMARY entity (highest-tier one it mentions)
    instead of treating all entity mentions as equal.
    """
    return {
        "url": 4,
        "domain": 3,
        "ascii_id": 2,
        "cjk_bigram": 1,
    }.get(ent_type, 0)


async def _build_entity_anchored_components(
    svc: "MemoryService",
) -> list[set[str]]:
    """Wave-32+ entity-anchored clustering — replaces naive
    connected-components when the entity index is populated.

    Why: naive components over the SAME_TOPIC edge set merge clusters
    whenever ANY edge bridges them. With many weak CJK-bigram bridges
    in the graph (every fact mentioning "网址" or "账号" links to
    every other), the whole graph collapses into one huge blob.

    Entity-anchored fix: group facts by their HIGHEST-TIER shared
    entity. A fact mentioning a URL + a bunch of CJK bigrams is
    anchored to the URL, not the bigrams. Two facts cluster only if
    they share a sufficiently-distinctive entity (URL / domain /
    distinct ASCII id), not just a common Chinese noun.

    Returns connected components in the same shape as the legacy
    path so the caller doesn't have to change. Empty list when the
    entity index has too few entries to be useful (caller falls
    back to the legacy path)."""
    try:
        from xmclaw.memory.v2.entity import get_entity_store
        store = get_entity_store()
    except Exception:  # noqa: BLE001
        return []
    stats = store.stats()
    # Require some entity coverage before relying on this path —
    # an empty index would cluster nothing.
    if stats.get("facts_indexed", 0) < 5:
        return []

    # Pull all facts; only care about ids.
    all_facts = await svc._vec.search(None, where=None, limit=2000)
    fact_ids = [f.id for f in all_facts if not f.superseded_by]
    if not fact_ids:
        return []

    # Step 1: compute each fact's primary entity (highest-tier
    # mention). Facts with no entity mentions go to the "isolated"
    # bucket — keep them out of clustering so they stay as
    # singletons + don't pollute neighbor clusters.
    primary_anchor: dict[str, str] = {}
    for fid in fact_ids:
        ents = store.entities_for_fact(fid)
        if not ents:
            continue
        best = max(ents, key=lambda e: _entity_tier(e.type))
        # Skip facts whose only entity is a bigram — too noisy to
        # anchor clustering on.
        if _entity_tier(best.type) < 2:
            continue
        primary_anchor[fid] = best.id

    # Step 2: invert — group fact_ids by their primary entity_id.
    by_anchor: dict[str, set[str]] = {}
    for fid, eid in primary_anchor.items():
        by_anchor.setdefault(eid, set()).add(fid)

    # Step 3: keep clusters with ≥ _MIN_CLUSTER_SIZE members.
    components = [s for s in by_anchor.values() if len(s) >= _MIN_CLUSTER_SIZE]
    return components


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


def _compute_cluster_hash(member_fact_ids: set[str]) -> str:
    """Wave-32+ deterministic cluster id (legacy / fallback).

    Stable hash over the sorted set of ALL member fact_ids. Used
    when we don't have access to Fact rows (Fact.evidence_count is
    what core-member hashing needs).

    KNOWN LIMIT: membership-sensitive. Adding ONE peripheral fact
    to a 10-fact cluster changes the hash → would re-trigger LLM
    naming. The new ``_compute_core_member_hash`` below is the
    preferred path; this remains for compatibility with callers
    that only have ids.
    """
    import hashlib
    if not member_fact_ids:
        return "empty"
    joined = "|".join(sorted(member_fact_ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


# Wave-32+ Chunk 7: How many top-evidence members anchor a cluster's
# identity. Peripheral members beyond top-K can be added / removed
# without changing the cluster's hash → cluster ID is stable across
# small membership changes. 5 is a heuristic — gives enough mass
# for the hash to be specific while letting weak members come and go.
_CORE_MEMBER_K: int = 5


def _compute_core_member_hash(facts: "list[Fact]", *, k: int = _CORE_MEMBER_K) -> str:
    """Wave-32+ Chunk 7: cluster hash robust to peripheral changes.

    Sort members by evidence_count descending, take the top-K, hash
    THEIR ids in sorted order. Properties:

      * Adding a fact with LOW evidence to a cluster → its id
        doesn't reach the top-K → hash unchanged
      * Removing a fact with LOW evidence → core unchanged → hash
        unchanged
      * A fact gains evidence and rises into top-K → hash changes
        (correctly — the cluster's identity actually shifted)
      * Re-running on the SAME data → same top-K (sort is stable
        for ties via id as secondary key) → same hash

    This is the right ID stability for the topic-naming use case:
    a stable conversation topic keeps its name even as new related
    facts trickle in.

    Falls back to the legacy full-membership hash when ``facts`` has
    no evidence_count info (e.g. minimal Fact stubs in tests)."""
    import hashlib
    if not facts:
        return "empty"
    # Sort by evidence_count desc, id asc (stable tiebreak).
    sorted_facts = sorted(
        facts,
        key=lambda f: (
            -(getattr(f, "evidence_count", 0) or 0),
            getattr(f, "id", ""),
        ),
    )
    core = sorted_facts[:k]
    core_ids = sorted(getattr(f, "id", "") for f in core if getattr(f, "id", ""))
    if not core_ids:
        return "empty"
    joined = "|".join(core_ids)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


async def _cluster_has_topic_node_by_hash(
    svc: "MemoryService", cluster_hash: str,
) -> bool:
    """O(N_topics) prefix scan to find an existing topic fact for
    a given cluster_hash. Much more reliable than the membership-
    sample check — even if a few PART_OF edges got lost, the topic
    fact itself carries the hash in its text."""
    try:
        topics = await svc._vec.search(
            None, where=f"kind = '{FactKind.TOPIC.value}'", limit=200,
        )
    except Exception:  # noqa: BLE001
        return False
    needle = f"topic:{cluster_hash}:"
    for t in topics:
        if not t.superseded_by and (t.text or "").startswith(needle):
            return True
    return False


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
    # Wave-32+ entity-anchored clustering: prefer the entity-tier
    # grouping when the entity index is populated. Falls back to the
    # legacy SAME_TOPIC-edge connected-components when the index is
    # empty (test contexts, fresh installs that never ran the
    # backfill yet).
    components = await _build_entity_anchored_components(svc)
    if not components:
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
        # Fetch Fact rows FIRST so we can compute the core-member
        # hash (Wave-32+ Chunk 7). Pre-fix the hash was over full
        # membership → adding 1 new peripheral fact to a cluster
        # changed the hash → re-triggered LLM naming on every
        # tick. Core-member hashing pins identity to the top-K
        # most-evidenced members; peripheral changes don't shift it.
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
        # Core-member hash for identity. The full-membership hash is
        # ALSO checked as a fallback so legacy topics (named before
        # this commit) still get detected and not re-created.
        cluster_hash = _compute_core_member_hash(facts)
        legacy_hash = _compute_cluster_hash(comp)
        if await _cluster_has_topic_node_by_hash(svc, cluster_hash):
            skipped += 1
            continue
        if await _cluster_has_topic_node_by_hash(svc, legacy_hash):
            skipped += 1
            continue
        if await _cluster_has_topic_node(svc, comp):
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
        # Write the topic fact. Wave-32+ stable id: text encodes the
        # cluster_hash so re-running on the same membership produces
        # the SAME fact_id (via Fact.compute_id which hashes text).
        # Net effect: idempotent topic naming — clicking 起主题名
        # twice on the same data is a no-op, not a duplicate-node
        # creator.
        try:
            topic_fact = await svc.remember(
                f"topic:{cluster_hash}:{name}",
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
