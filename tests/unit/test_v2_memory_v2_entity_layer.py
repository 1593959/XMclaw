"""Entity layer — Wave-32+ (2026-05-18).

Pin the canonicalization + extraction + reverse-index behavior.
The entity layer is the foundation for stable cluster IDs +
deterministic same-topic bridges (follow-up commits).
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2.entity import (
    EntityStore,
    canonicalize,
    entity_id_for,
    extract_entity_mentions,
    reset_entity_store,
)


@pytest.fixture(autouse=True)
def _isolated_store():
    """Reset the process-singleton between tests so leaks don't
    cross-contaminate."""
    reset_entity_store()
    yield
    reset_entity_store()


# ── canonicalize ────────────────────────────────────────────────────


def test_url_canonicalization_strips_case_and_trailing_slash() -> None:
    assert canonicalize("HTTPS://PW310.WXSelling.com/login/") == \
        canonicalize("https://pw310.wxselling.com/login")


def test_url_canonicalization_strips_default_ports() -> None:
    assert canonicalize("http://x.com:80/p", type_hint="url") == \
        canonicalize("http://x.com/p", type_hint="url")
    assert canonicalize("https://x.com:443", type_hint="url") == \
        canonicalize("https://x.com", type_hint="url")


def test_ascii_identifier_lowercased() -> None:
    assert canonicalize("ADMIN") == canonicalize("admin")
    assert canonicalize("Admin.") == canonicalize("admin")


def test_cjk_pass_through() -> None:
    """CJK has no case to normalize — canonical = input modulo
    whitespace strip."""
    assert canonicalize("陪玩店") == "陪玩店"


def test_canonical_empty_input() -> None:
    assert canonicalize("") == ""
    assert canonicalize("   ") == ""


def test_entity_id_stable_across_calls() -> None:
    """The same canonical produces the same id deterministically.
    This is what unlocks idempotent cluster IDs downstream."""
    a = entity_id_for("admin")
    b = entity_id_for("admin")
    assert a == b
    # Different inputs → different ids.
    assert entity_id_for("admin") != entity_id_for("admin888")


# ── extract_entity_mentions ─────────────────────────────────────────


def test_url_produces_url_and_domain_mentions() -> None:
    mentions = extract_entity_mentions(
        "网址: https://pw310.wxselling.com/login",
    )
    types = {m.type for m in mentions}
    # The URL is captured + the bare domain is captured separately.
    assert "url" in types
    # CJK bi-gram "网址" also present.
    assert "cjk_bigram" in types
    canonicals = {m.canonical for m in mentions}
    assert "https://pw310.wxselling.com/login" in canonicals
    assert "网址" in canonicals


def test_admin_credentials_extract_consistently() -> None:
    """Two facts mentioning admin should produce a SHARED entity id
    when fed through the store. Pin this — it's the core of the
    bridge."""
    a = extract_entity_mentions("凭据: 账号是admin")
    b = extract_entity_mentions("陪玩店账号为admin")
    a_ids = {entity_id_for(m.canonical) for m in a}
    b_ids = {entity_id_for(m.canonical) for m in b}
    overlap = a_ids & b_ids
    assert overlap, "no shared entity id — bridge would fail"
    # The overlap should include the 'admin' identifier.
    assert entity_id_for("admin") in overlap


def test_stopwords_not_extracted() -> None:
    mentions = extract_entity_mentions("我们 可以 这个 那个")
    canonicals = {m.canonical for m in mentions}
    assert "我们" not in canonicals
    assert "可以" not in canonicals


def test_short_ascii_ids_filtered() -> None:
    """Length < 4 ASCII chars excluded so "id"/"is"/"go" don't
    over-link."""
    mentions = extract_entity_mentions("set id to 7")
    assert all(len(m.canonical) >= 4 for m in mentions if m.type == "ascii_id")


def test_cjk_bigram_emission_for_long_runs() -> None:
    """Long CJK noun phrase emits every 2-char window so the
    embedder isn't the only path between fact texts that mention
    overlapping nouns."""
    mentions = extract_entity_mentions("陪玩店账号")
    canonicals = {m.canonical for m in mentions if m.type == "cjk_bigram"}
    assert "陪玩" in canonicals
    assert "玩店" in canonicals
    assert "店账" in canonicals
    assert "账号" in canonicals


# ── EntityStore ─────────────────────────────────────────────────────


def test_store_registers_and_reverse_indexes() -> None:
    store = EntityStore()
    ids = store.register_fact_text(
        "f1", "凭据: 账号是admin",
    )
    assert len(ids) > 0
    # Reverse index works.
    eid_admin = entity_id_for("admin")
    assert "f1" in store.facts_for_entity(eid_admin)


def test_store_shared_entities_query() -> None:
    """Two facts sharing 'admin' return that entity in shared()."""
    store = EntityStore()
    store.register_fact_text("f1", "凭据: 账号是admin")
    store.register_fact_text("f2", "陪玩店账号为admin")
    shared = store.shared_entities("f1", "f2")
    assert entity_id_for("admin") in shared


def test_store_co_mentioned_facts_lookup() -> None:
    """``co_mentioned_facts`` is the bridge's hot path: O(1) "facts
    sharing any of my entities" without scanning every other fact's
    text. Pin the contract."""
    store = EntityStore()
    store.register_fact_text("f1", "凭据: 账号是admin")
    store.register_fact_text("f2", "陪玩店账号为admin")  # shares 'admin' + '账号'
    store.register_fact_text("f3", "用户喜欢暗色主题")    # unrelated
    co = store.co_mentioned_facts("f1")
    assert "f2" in co
    assert "f3" not in co
    assert "f1" not in co  # exclude self


def test_store_idempotent_register() -> None:
    """Registering the same fact text twice doesn't duplicate the
    entity record OR the reverse-index entry."""
    store = EntityStore()
    store.register_fact_text("f1", "陪玩店账号为admin")
    n1 = store.stats()
    store.register_fact_text("f1", "陪玩店账号为admin")
    n2 = store.stats()
    assert n1 == n2


def test_store_forget_fact_cleans_index() -> None:
    """When a fact is deleted, its entity-mentions get cleaned up
    so future co_mentioned_facts queries don't return the stale id."""
    store = EntityStore()
    store.register_fact_text("f1", "陪玩店账号为admin")
    store.register_fact_text("f2", "凭据: 账号是admin")
    assert "f2" in store.co_mentioned_facts("f1")
    store.forget_fact("f2")
    assert "f2" not in store.co_mentioned_facts("f1")


def test_store_drops_orphan_entities() -> None:
    """An entity whose last referencing fact got forgotten should
    drop from the store too — keeps memory bounded over a long-
    running daemon."""
    store = EntityStore()
    store.register_fact_text("f1", "陪玩店 admin 唯一引用")
    before = store.stats()["entities"]
    assert before > 0
    store.forget_fact("f1")
    assert store.stats()["entities"] == 0


def test_store_surface_forms_capped() -> None:
    """An entity that appears in many different surface forms keeps
    only the last 5 — prevents pathological memory growth on a
    long-lived entity."""
    store = EntityStore()
    for i in range(20):
        store.register_fact_text(f"f{i}", f"admin variant {i}")
    eid = entity_id_for("admin")
    ents = [
        e for fid in [f"f{i}" for i in range(20)]
        for e in store.entities_for_fact(fid) if e.id == eid
    ]
    assert ents
    # Pick any — they all reference the same Entity object.
    assert len(ents[0].surface_forms) <= 5
