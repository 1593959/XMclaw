"""LLM-assisted topic operations — Wave-32+ (2026-05-18).

Pin Layer-2 (LLM-judged SAME_TOPIC) + Layer-3 (LLM cluster naming).

The LLM is stubbed with a recorder so:
  * we verify the right prompt was built (assertion on stub input)
  * the response is deterministic for the test
  * no real network calls
"""
from __future__ import annotations

from xmclaw.memory.v2.llm_topic import (
    _build_naming_prompt,
    _build_refine_prompt,
    _clean_topic_name,
    _parse_refine_response,
)
from xmclaw.memory.v2.models import Fact, FactKind


# ── prompt builders ────────────────────────────────────────────────


def _mk_fact(text: str, kind: str = "project", **kw) -> Fact:
    return Fact(
        id=kw.get("id", f"f_{hash(text) & 0xffff:x}"),
        kind=kind, scope=kw.get("scope", "project"),
        text=text,
        confidence=kw.get("confidence", 0.9),
        embedding=tuple([0.0] * 8),
        evidence_count=1,
    )


def test_refine_prompt_includes_all_pair_texts() -> None:
    pairs = [
        (_mk_fact("网址: https://pw310"), _mk_fact("目标网站无需验证")),
        (_mk_fact("账号是admin"), _mk_fact("陪玩店账号为admin")),
    ]
    prompt = _build_refine_prompt(pairs)
    assert "pw310" in prompt
    assert "目标网站" in prompt
    assert "admin" in prompt
    # Schema directive present so the LLM doesn't free-form prose.
    assert "JSON" in prompt or "json" in prompt
    # Index labels for every pair so the response can be aligned.
    assert "  0." in prompt
    assert "  1." in prompt


def test_naming_prompt_truncates_huge_clusters() -> None:
    facts = [_mk_fact(f"fact #{i}") for i in range(50)]
    prompt = _build_naming_prompt(facts)
    # Prompt should mention "还有 N 条" for the truncated tail.
    assert "还有" in prompt
    # First 30 should be in the prompt.
    assert "fact #0" in prompt
    # Last few should NOT be in the prompt body (only mentioned as count).
    assert "fact #49" not in prompt


# ── response parsing ───────────────────────────────────────────────


def test_parse_refine_handles_plain_json_array() -> None:
    out = _parse_refine_response("[1, 0, 1, 1, 0]", 5)
    assert out == [True, False, True, True, False]


def test_parse_refine_extracts_array_from_chatty_response() -> None:
    """Some models prepend explanation despite the prompt directive.
    The regex grabs the first JSON-looking array from the response."""
    out = _parse_refine_response(
        "Sure! Here you go: [1, 0, 1]. Let me know if you need more.",
        3,
    )
    assert out == [True, False, True]


def test_parse_refine_returns_all_false_on_garbage() -> None:
    """Bad/no JSON → safe fallback (no spurious edges)."""
    assert _parse_refine_response("not json", 3) == [False, False, False]
    assert _parse_refine_response("", 4) == [False, False, False, False]


def test_parse_refine_pads_short_response() -> None:
    """LLM returned 2 values but we asked for 4 → pad with False."""
    out = _parse_refine_response("[1, 1]", 4)
    assert out == [True, True, False, False]


def test_parse_refine_clamps_long_response() -> None:
    out = _parse_refine_response("[1, 1, 1, 1, 1, 1]", 2)
    assert out == [True, True]


# ── topic name cleaning ────────────────────────────────────────────


def test_clean_topic_name_strips_quotes() -> None:
    assert _clean_topic_name('"项目凭据"') == "项目凭据"
    assert _clean_topic_name("「陪玩店」") == "陪玩店"
    assert _clean_topic_name("'foo'") == "foo"


def test_clean_topic_name_first_line_only() -> None:
    """Models sometimes return a name + explanation. Take line 1."""
    assert _clean_topic_name("陪玩店凭据\n这个标题代表...") == "陪玩店凭据"


def test_clean_topic_name_caps_length() -> None:
    """A 30-char name from a misbehaving model gets clamped."""
    long_raw = "这是一个非常非常长的主题名字超过了12个字"
    assert len(_clean_topic_name(long_raw)) <= 12


def test_clean_topic_name_returns_empty_for_empty_input() -> None:
    assert _clean_topic_name("") == ""
    assert _clean_topic_name("   \n  ") == ""
    assert _clean_topic_name('""') == ""


# ── FactKind.TOPIC registered ──────────────────────────────────────


def test_topic_fact_kind_registered() -> None:
    """The new TOPIC kind must be in the enum so remember()
    write paths recognise it. Pins the addition so a future
    refactor that drops the enum value gets caught."""
    assert FactKind.TOPIC.value == "topic"
    assert "topic" in {k.value for k in FactKind}


# ── stable cluster id (Wave-32+ chunk 2) ───────────────────────────


def test_compute_cluster_hash_stable_across_calls() -> None:
    """The same membership → same hash. Two calls in the same
    process must agree."""
    from xmclaw.memory.v2.llm_topic import _compute_cluster_hash
    a = _compute_cluster_hash({"f1", "f2", "f3"})
    b = _compute_cluster_hash({"f3", "f2", "f1"})  # order shouldn't matter
    assert a == b


def test_compute_cluster_hash_membership_sensitive() -> None:
    """Different membership → different hash. Pin that adding /
    removing a single member changes the cluster identity."""
    from xmclaw.memory.v2.llm_topic import _compute_cluster_hash
    base = _compute_cluster_hash({"f1", "f2", "f3"})
    added = _compute_cluster_hash({"f1", "f2", "f3", "f4"})
    removed = _compute_cluster_hash({"f1", "f2"})
    assert base != added
    assert base != removed
    assert added != removed


def test_compute_cluster_hash_empty_input() -> None:
    """An empty set returns a defined sentinel rather than raising
    so callers don't have to special-case the (rare) zero-member
    edge case."""
    from xmclaw.memory.v2.llm_topic import _compute_cluster_hash
    assert _compute_cluster_hash(set()) == "empty"


def test_compute_cluster_hash_length_bounded() -> None:
    """Hash output is short enough to fit in fact text without
    bloating the LanceDB row size. Pin at 12 chars."""
    from xmclaw.memory.v2.llm_topic import _compute_cluster_hash
    h = _compute_cluster_hash({"f1", "f2"})
    assert len(h) == 12


# ── entity-tier ranking (Wave-32+ chunk C) ──────────────────────────


def test_entity_tier_url_beats_bigram() -> None:
    """URL is the most distinctive anchor; CJK bigram is the
    weakest. Pin the ordering — clustering quality depends on it."""
    from xmclaw.memory.v2.llm_topic import _entity_tier
    assert _entity_tier("url") > _entity_tier("domain")
    assert _entity_tier("domain") > _entity_tier("ascii_id")
    assert _entity_tier("ascii_id") > _entity_tier("cjk_bigram")
    assert _entity_tier("cjk_bigram") > _entity_tier("unknown_type")


def test_entity_tier_unknown_returns_zero() -> None:
    from xmclaw.memory.v2.llm_topic import _entity_tier
    assert _entity_tier("") == 0
    assert _entity_tier("anything_else") == 0


# ── core-member cluster hash (Wave-32+ chunk 7) ─────────────────────


def test_core_member_hash_stable_to_peripheral_additions() -> None:
    """Adding a LOW-evidence fact to a cluster shouldn't change its
    identity. Pin this — it's the whole point of the new hash."""
    from xmclaw.memory.v2.llm_topic import _compute_core_member_hash
    # Base: 5 high-evidence facts.
    base_facts = [
        _mk_fact("text a", id=f"f{i}", confidence=0.9) for i in range(5)
    ]
    # Give each high evidence_count.
    for i, f in enumerate(base_facts):
        object.__setattr__(f, "evidence_count", 10 - i)
    base_hash = _compute_core_member_hash(base_facts)

    # Add 1 low-evidence peripheral fact (evidence_count=1).
    peripheral = _mk_fact("peripheral", id="periph")
    object.__setattr__(peripheral, "evidence_count", 1)
    new_hash = _compute_core_member_hash(base_facts + [peripheral])
    assert base_hash == new_hash, "peripheral fact shifted the hash"


def test_core_member_hash_changes_when_high_evidence_added() -> None:
    """A high-evidence fact that displaces a current core member
    SHOULD change the hash — the cluster's center of mass actually
    shifted. Pin so we don't over-correct into total insensitivity."""
    from xmclaw.memory.v2.llm_topic import _compute_core_member_hash
    base_facts = [
        _mk_fact("text", id=f"f{i}") for i in range(5)
    ]
    for i, f in enumerate(base_facts):
        object.__setattr__(f, "evidence_count", 5 - i)  # 5..1
    base_hash = _compute_core_member_hash(base_facts)
    # Add a fact with evidence_count=20 — must displace f4 (count=1)
    # in the top-5.
    dominant = _mk_fact("dominant", id="dom")
    object.__setattr__(dominant, "evidence_count", 20)
    new_hash = _compute_core_member_hash(base_facts + [dominant])
    assert base_hash != new_hash


def test_core_member_hash_robust_to_low_evidence_removal() -> None:
    """Removing a peripheral fact also shouldn't shift the cluster
    identity. Symmetric to the addition case."""
    from xmclaw.memory.v2.llm_topic import _compute_core_member_hash
    facts = [_mk_fact("t", id=f"f{i}") for i in range(7)]
    for i, f in enumerate(facts):
        object.__setattr__(f, "evidence_count", 10 - i)  # 10..4
    full_hash = _compute_core_member_hash(facts)
    # Remove f5 / f6 (lowest evidence) — outside core (top-5).
    pruned = facts[:5]
    pruned_hash = _compute_core_member_hash(pruned)
    assert full_hash == pruned_hash


def test_core_member_hash_deterministic_across_calls() -> None:
    """Same input → same hash. Used to detect re-runs of identical
    clusters."""
    from xmclaw.memory.v2.llm_topic import _compute_core_member_hash
    facts = [_mk_fact("t", id=f"f{i}") for i in range(3)]
    for f in facts:
        object.__setattr__(f, "evidence_count", 5)
    a = _compute_core_member_hash(facts)
    b = _compute_core_member_hash(facts)
    assert a == b


def test_core_member_hash_empty_input() -> None:
    from xmclaw.memory.v2.llm_topic import _compute_core_member_hash
    assert _compute_core_member_hash([]) == "empty"


def test_core_member_hash_length_bounded() -> None:
    from xmclaw.memory.v2.llm_topic import _compute_core_member_hash
    f = _mk_fact("t", id="f1")
    object.__setattr__(f, "evidence_count", 1)
    assert len(_compute_core_member_hash([f])) == 12


# ── modularity refinement (Wave-32+ chunk 9) ────────────────────────


def test_refine_returns_cluster_unchanged_when_small() -> None:
    """Below 2× _MIN_CLUSTER_SIZE we shouldn't even try to split —
    not enough mass for the move loop to find meaningful sub-
    communities."""
    from xmclaw.memory.v2.llm_topic import _refine_cluster_by_modularity
    cluster = {"f1", "f2", "f3"}  # 3 = _MIN_CLUSTER_SIZE, below 2×
    edges = [("f1", "f2", 0.5), ("f2", "f3", 0.5)]
    result = _refine_cluster_by_modularity(cluster, edges)
    assert result == [cluster]


def test_refine_splits_two_dense_sub_communities() -> None:
    """Classic Louvain test case: two cliques connected by a single
    weak bridge edge. Modularity move loop should split them."""
    from xmclaw.memory.v2.llm_topic import _refine_cluster_by_modularity
    # Group A: f1-f2-f3 fully connected (strong)
    # Group B: f4-f5-f6 fully connected (strong)
    # Bridge: f3-f4 (weak)
    cluster = {"f1", "f2", "f3", "f4", "f5", "f6"}
    edges = [
        ("f1", "f2", 1.0), ("f1", "f3", 1.0), ("f2", "f3", 1.0),
        ("f4", "f5", 1.0), ("f4", "f6", 1.0), ("f5", "f6", 1.0),
        ("f3", "f4", 0.1),
    ]
    result = _refine_cluster_by_modularity(cluster, edges)
    # Should produce two sub-clusters.
    assert len(result) == 2
    # Each contains one full clique.
    a_cluster = next(r for r in result if "f1" in r)
    b_cluster = next(r for r in result if "f4" in r)
    assert a_cluster == {"f1", "f2", "f3"}
    assert b_cluster == {"f4", "f5", "f6"}


def test_refine_keeps_cohesive_cluster_intact() -> None:
    """Fully connected cluster — no internal structure to split
    on. Should stay as one."""
    from xmclaw.memory.v2.llm_topic import _refine_cluster_by_modularity
    cluster = {"f1", "f2", "f3", "f4", "f5", "f6"}
    edges = []
    nodes = list(cluster)
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            edges.append((a, b, 1.0))
    result = _refine_cluster_by_modularity(cluster, edges)
    assert len(result) == 1
    assert result[0] == cluster


def test_refine_no_edges_returns_intact() -> None:
    """Cluster with zero internal edges (all connections to it are
    cross-cluster). The move loop has nothing to work with."""
    from xmclaw.memory.v2.llm_topic import _refine_cluster_by_modularity
    cluster = {"f1", "f2", "f3", "f4", "f5", "f6"}
    result = _refine_cluster_by_modularity(cluster, [])
    assert result == [cluster]


def test_refine_folds_too_small_residue_into_largest() -> None:
    """When the move loop produces a tiny side-community below
    _MIN_CLUSTER_SIZE, those nodes fold back into the largest
    sub-cluster instead of being dropped entirely. Otherwise we'd
    lose data."""
    from xmclaw.memory.v2.llm_topic import _refine_cluster_by_modularity
    # 5 fully connected + 1 isolated weak connection to one of them.
    cluster = {"f1", "f2", "f3", "f4", "f5", "iso"}
    edges = [
        ("f1", "f2", 1.0), ("f1", "f3", 1.0), ("f1", "f4", 1.0),
        ("f1", "f5", 1.0),
        ("f2", "f3", 1.0), ("f2", "f4", 1.0), ("f2", "f5", 1.0),
        ("f3", "f4", 1.0), ("f3", "f5", 1.0), ("f4", "f5", 1.0),
        ("f1", "iso", 0.1),
    ]
    result = _refine_cluster_by_modularity(cluster, edges)
    # All 6 nodes accounted for (no data loss via fold-in).
    flat = set()
    for r in result:
        flat |= r
    assert flat == cluster


def test_build_within_cluster_adjacency_filters_cross_edges() -> None:
    """Edges with one endpoint outside the cluster are dropped from
    the adjacency. Without this, modularity calc would include
    weights against non-members and skew the move scores."""
    from xmclaw.memory.v2.llm_topic import _build_within_cluster_adjacency
    cluster = {"f1", "f2"}
    edges = [
        ("f1", "f2", 1.0),    # within
        ("f1", "outside", 1.0),  # crosses boundary
        ("outside", "f2", 1.0),  # crosses boundary
    ]
    adj = _build_within_cluster_adjacency(cluster, edges)
    assert adj["f1"] == {"f2": 1.0}
    assert adj["f2"] == {"f1": 1.0}
    assert "outside" not in adj
