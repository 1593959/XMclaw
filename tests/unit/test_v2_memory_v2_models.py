"""Phase 1a — L1 Fact + Relation model unit tests.

Pure data-model tests. No backend, no I/O. Covers:
    * deterministic id derivation (same content ⇒ same id)
    * to_dict / from_dict round-trip
    * embedding optional handling
    * relation id derivation
"""
from __future__ import annotations

from xmclaw.memory.v2 import (
    Fact,
    FactKind,
    FactScope,
    Relation,
    RelationKind,
)


# ── Fact id derivation ────────────────────────────────────────────


def test_compute_id_is_deterministic() -> None:
    fid_a = Fact.compute_id(
        kind=FactKind.PROJECT, scope=FactScope.PROJECT,
        text="陪玩店 pw310.wxselling.com",
    )
    fid_b = Fact.compute_id(
        kind=FactKind.PROJECT, scope=FactScope.PROJECT,
        text="陪玩店 pw310.wxselling.com",
    )
    assert fid_a == fid_b
    assert fid_a.startswith("project:project:")
    assert len(fid_a.split(":")[-1]) == 12  # 12-hex-char hash


def test_compute_id_normalises_whitespace() -> None:
    """Trivial reformat shouldn't fork into a new fact."""
    a = Fact.compute_id(
        kind="preference", scope="user", text="用户  喜欢   简短回复",
    )
    b = Fact.compute_id(
        kind="preference", scope="user", text="用户 喜欢 简短回复",
    )
    assert a == b


def test_compute_id_different_kind_diverges() -> None:
    a = Fact.compute_id(
        kind=FactKind.PREFERENCE, scope=FactScope.USER, text="X",
    )
    b = Fact.compute_id(
        kind=FactKind.IDENTITY, scope=FactScope.USER, text="X",
    )
    assert a != b


def test_compute_id_accepts_str_kind_and_enum() -> None:
    """Same logical key whether caller passed str or enum."""
    a = Fact.compute_id(kind="project", scope="project", text="X")
    b = Fact.compute_id(
        kind=FactKind.PROJECT, scope=FactScope.PROJECT, text="X",
    )
    assert a == b


# ── Fact round-trip ───────────────────────────────────────────────


def test_fact_to_from_dict_roundtrip() -> None:
    fid = Fact.compute_id(kind="project", scope="project", text="陪玩店")
    f = Fact(
        id=fid, kind="project", scope="project", text="陪玩店",
        confidence=0.95, evidence_count=3,
        embedding=(0.1, 0.2, 0.3),
        source_event_id="ev-abc123",
        contradicts=("project:project:000000abcdef",),
        layer="long_term",
    )
    d = f.to_dict()
    f2 = Fact.from_dict(d)
    assert f2.id == f.id
    assert f2.text == f.text
    assert f2.confidence == f.confidence
    assert f2.embedding == (0.1, 0.2, 0.3)
    assert f2.source_event_id == "ev-abc123"
    assert f2.contradicts == ("project:project:000000abcdef",)
    assert f2.layer == "long_term"


def test_fact_embedding_optional() -> None:
    """Fact without embedding round-trips with None preserved."""
    f = Fact(
        id="x:y:abcdef000001", kind="preference", scope="user",
        text="hi", embedding=None,
    )
    d = f.to_dict()
    assert d["embedding"] is None
    f2 = Fact.from_dict(d)
    assert f2.embedding is None


# ── Relation id + round-trip ──────────────────────────────────────


def test_relation_compute_id() -> None:
    rid = Relation.compute_id(
        source_fact_id="a", target_fact_id="b",
        relation=RelationKind.CONTRADICTS,
    )
    assert rid == "CONTRADICTS:a->b"
    # Same with string-form input.
    rid2 = Relation.compute_id(
        source_fact_id="a", target_fact_id="b",
        relation="CONTRADICTS",
    )
    assert rid == rid2


def test_relation_roundtrip() -> None:
    r = Relation(
        id="CAUSED_BY:fact1->event:ev1",
        source_fact_id="fact1",
        target_fact_id="event:ev1",
        relation="CAUSED_BY",
        strength=0.9,
        auto_extracted=True,
        ts=1700000000.0,
    )
    d = r.to_dict()
    r2 = Relation.from_dict(d)
    assert r2 == r


# ── Enum values match Literal types ───────────────────────────────


def test_factkind_values_cover_canonical_set() -> None:
    # Wave-27 follow-up: ``lesson`` (Phase 2) + ``persona_manual``
    # (Phase 3b) joined the canonical set.
    assert {k.value for k in FactKind} == {
        "preference", "decision", "identity",
        "commitment", "correction", "project", "episode",
        "lesson", "persona_manual",
    }


def test_relationkind_values_cover_six_kinds() -> None:
    assert {k.value for k in RelationKind} == {
        "CONTRADICTS", "SUPERSEDES", "CAUSED_BY",
        "PART_OF", "REFERS_TO", "SAME_TOPIC",
    }
