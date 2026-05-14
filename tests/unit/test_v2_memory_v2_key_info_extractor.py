"""Phase 3 — KeyInfoExtractor regex pattern tests.

THE acceptance test for "user gives business info → it lands in
memory deterministically, without LLM call, without agent
discretion". Each user-realistic input is tested + the resulting
extraction is asserted explicitly.
"""
from __future__ import annotations

import pytest

from xmclaw.memory.v2 import (
    EmbeddingService,
    InMemoryGraphBackend,
    InMemoryVectorBackend,
    MemoryService,
    StubEmbedder,
)
from xmclaw.memory.v2.key_info_extractor import (
    extract_and_remember,
    extract_keys,
)


# ── URL extraction ────────────────────────────────────────────────


def test_extract_http_url() -> None:
    keys = extract_keys("看一下 https://pw310.wxselling.com/admin/order")
    urls = [k for k in keys if k.pattern_name == "url"]
    assert len(urls) == 1
    assert "pw310.wxselling.com" in urls[0].text
    assert urls[0].kind == "project"
    assert urls[0].scope == "project"


def test_extract_url_strips_trailing_punct() -> None:
    keys = extract_keys("访问 https://example.com/page.")
    urls = [k for k in keys if k.pattern_name == "url"]
    assert urls[0].text.endswith("example.com/page")


def test_extract_multiple_urls() -> None:
    keys = extract_keys(
        "前端 https://app.example.com 后端 https://api.example.com",
    )
    urls = [k for k in keys if k.pattern_name == "url"]
    assert len(urls) == 2


# ── Credentials ───────────────────────────────────────────────────


def test_extract_chinese_account_password() -> None:
    msg = "账号 admin，密码 admin888"
    keys = extract_keys(msg)
    creds = [k for k in keys if k.pattern_name == "credential"]
    assert len(creds) >= 2
    texts = " ".join(c.text for c in creds)
    assert "admin" in texts
    assert "admin888" in texts


def test_extract_english_credentials() -> None:
    msg = "username: admin, password: hunter2"
    keys = extract_keys(msg)
    creds = [k for k in keys if k.pattern_name == "credential"]
    assert len(creds) >= 2


def test_extract_cred_pair() -> None:
    """admin/admin888 shorthand pair."""
    keys = extract_keys("登录用 admin/admin888")
    pairs = [k for k in keys if k.pattern_name == "cred_pair"]
    assert len(pairs) == 1
    assert "admin" in pairs[0].text
    assert "admin888" in pairs[0].text


def test_cred_pair_skips_license_style() -> None:
    """MIT/Apache shouldn't trigger cred_pair (no digit, no known
    cred word). Stops the false-positive avalanche."""
    keys = extract_keys("License MIT/Apache 2.0")
    pairs = [k for k in keys if k.pattern_name == "cred_pair"]
    # MIT/Apache → no digit, neither word is in {admin/root/user/...}
    # so it should be skipped.
    assert len(pairs) == 0


# ── Business goals ────────────────────────────────────────────────


def test_extract_chinese_goal() -> None:
    keys = extract_keys("我们的目标是月流水破 5 万")
    goals = [k for k in keys if k.pattern_name == "goal"]
    assert len(goals) >= 1
    assert any("月流水" in g.text or "目标" in g.text for g in goals)


def test_extract_english_goal() -> None:
    keys = extract_keys("target $50K MRR next quarter")
    goals = [k for k in keys if k.pattern_name == "goal"]
    assert len(goals) >= 1


def test_extract_gmv_goal() -> None:
    keys = extract_keys("GMV 做到 200 万")
    goals = [k for k in keys if k.pattern_name == "goal"]
    assert len(goals) >= 1


# ── Explicit remember directive ───────────────────────────────────


def test_extract_remember_directive_chinese() -> None:
    keys = extract_keys("记住 用户喜欢深色主题")
    direct = [k for k in keys if k.pattern_name == "remember_directive"]
    assert len(direct) >= 1
    assert "深色主题" in direct[0].text
    assert direct[0].kind == "preference"


def test_extract_remember_directive_english() -> None:
    keys = extract_keys("from now on, always use Python 3.10+")
    direct = [k for k in keys if k.pattern_name == "remember_directive"]
    assert len(direct) >= 1


def test_extract_always_pattern() -> None:
    keys = extract_keys("以后都用 PowerShell 不要再用 bash")
    direct = [k for k in keys if k.pattern_name == "remember_directive"]
    assert len(direct) >= 1


# ── Identity / preference / correction ────────────────────────────


def test_extract_identity() -> None:
    keys = extract_keys("我是张三，专门做后端")
    ids = [k for k in keys if k.pattern_name == "identity"]
    assert len(ids) >= 1


def test_extract_preference_chinese() -> None:
    keys = extract_keys("我喜欢简短回复")
    prefs = [k for k in keys if k.pattern_name == "preference"]
    assert len(prefs) >= 1


def test_extract_correction() -> None:
    keys = extract_keys("不要再用 sqlite-vec 了")
    corrections = [k for k in keys if k.pattern_name == "correction"]
    assert len(corrections) >= 1


# ── Real-world combined message ───────────────────────────────────


def test_real_world_combined_message() -> None:
    """The actual user message that motivated this whole rewrite."""
    msg = (
        "我是干陪玩店的，网站是 https://pw310.wxselling.com/#/admin/order/index.html，"
        "账号 admin，密码 admin888，我们的目的是月流水破 5 万。"
        "记住这些。"
    )
    keys = extract_keys(msg)
    by_pattern = {k.pattern_name: k for k in keys}
    assert "url" in by_pattern
    assert "credential" in by_pattern
    assert "goal" in by_pattern
    # remember_directive matches "记住这些"
    assert "remember_directive" in by_pattern


def test_no_match_on_irrelevant_message() -> None:
    """Routine chitchat shouldn't produce any extractions."""
    keys = extract_keys("好的，明白了。")
    assert keys == []


def test_empty_message_returns_empty() -> None:
    assert extract_keys("") == []
    assert extract_keys("    ") == []


# ── Span deduplication ───────────────────────────────────────────


def test_overlapping_spans_collapse_to_outer_match() -> None:
    """Nested matches (e.g. URL contains a domain that another
    regex might pick up) shouldn't double-count."""
    msg = "网址 https://admin.example.com"
    keys = extract_keys(msg)
    # The URL should be one extraction. cred_pair shouldn't fire on
    # 'admin.example' (no digit, no slash separator), so this stays
    # at one extraction total.
    assert len(keys) == 1
    assert keys[0].pattern_name == "url"


# ── extract_and_remember end-to-end ───────────────────────────────


@pytest.mark.asyncio
async def test_extract_and_remember_writes_to_service() -> None:
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    msg = (
        "我是干陪玩店的，网站 https://pw310.wxselling.com，"
        "账号 admin，密码 admin888，月流水目标 5 万。"
    )
    written = await extract_and_remember(
        msg, svc, source_event_id="ev-test-001",
    )
    assert len(written) >= 4
    # Search for the URL fact.
    hits = await svc.recall("pw310", k=5)
    assert any("pw310" in h.fact.text for h in hits)
    # CAUSED_BY edge to the event exists on each fact.
    for f in written:
        nbrs = await svc.neighbors(f.id, relation_types=["CAUSED_BY"])
        assert any(t == "event:ev-test-001" for _, t in nbrs)


@pytest.mark.asyncio
async def test_extract_and_remember_idempotent_on_repeat() -> None:
    """Same message twice ⇒ same facts, evidence_count bumped."""
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    msg = "网站 https://example.com，账号 admin"
    w1 = await extract_and_remember(msg, svc)
    w2 = await extract_and_remember(msg, svc)
    # Same ids both times.
    ids1 = {f.id for f in w1}
    ids2 = {f.id for f in w2}
    assert ids1 == ids2
    # evidence_count on at least one fact is now 2.
    facts = [await svc.get_fact(fid) for fid in ids1]
    assert any(f.evidence_count == 2 for f in facts if f is not None)


@pytest.mark.asyncio
async def test_extract_and_remember_empty_message_noop() -> None:
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    written = await extract_and_remember("", svc)
    assert written == []
    assert await svc.count() == 0
