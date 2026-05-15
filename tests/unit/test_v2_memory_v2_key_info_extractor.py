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
async def test_extract_and_remember_links_co_extracted_facts() -> None:
    """Wave-27 fix-9: when ONE user message produces multiple facts
    (URL + account + password etc.), they get pairwise SAME_TOPIC
    edges. Reproduces the user complaint: "是这个网站的账号密码，
    但是为什么没有联系" — graph showed 3 disconnected nodes.
    """
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    msg = (
        "https://pw310.wxselling.com/我是开陪玩店的，"
        "这是我经营的网站，账号 admin，密码 admin888"
    )
    written = await extract_and_remember(
        msg, svc, source_event_id="ev-cooccur",
    )
    assert len(written) >= 3  # URL + account + password at minimum

    # Find the URL fact and one of the credential facts.
    url_fact = next(
        (f for f in written if "pw310.wxselling.com" in f.text), None,
    )
    cred_facts = [f for f in written if "凭据" in f.text or "admin" in f.text]
    assert url_fact is not None, "URL fact not extracted"
    assert len(cred_facts) >= 2, "credentials not extracted"

    # Every credential fact must have a SAME_TOPIC edge to the URL.
    for cred in cred_facts:
        nbrs = await svc.neighbors(cred.id, relation_types=["SAME_TOPIC"])
        nbr_ids = {target for _, target in nbrs}
        assert url_fact.id in nbr_ids, (
            f"credential fact {cred.text!r} has no SAME_TOPIC edge to "
            f"the URL — co-occurrence linking broken"
        )

    # And the reverse direction: URL → credential (symmetric).
    url_nbrs = await svc.neighbors(url_fact.id, relation_types=["SAME_TOPIC"])
    url_nbr_ids = {target for _, target in url_nbrs}
    for cred in cred_facts:
        assert cred.id in url_nbr_ids, (
            f"URL has no SAME_TOPIC edge to {cred.text!r} — "
            "expected symmetric co-occurrence link"
        )


@pytest.mark.asyncio
async def test_extract_and_remember_single_fact_no_self_link() -> None:
    """A message that yields only ONE fact must not create any
    self-edges (sanity check on the pairwise loop)."""
    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    msg = "https://only-a-url.com"
    written = await extract_and_remember(msg, svc, source_event_id="ev-1")
    assert len(written) == 1
    nbrs = await svc.neighbors(written[0].id, relation_types=["SAME_TOPIC"])
    assert nbrs == []


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


# ── Phase 3.1 — extended coverage ────────────────────────────────


def test_extract_email() -> None:
    keys = extract_keys("联系 alice@example.com 或者 bob.smith+filter@sub.example.co.uk")
    emails = [k for k in keys if k.pattern_name == "email"]
    assert len(emails) == 2


def test_extract_phone_cn_mobile() -> None:
    keys = extract_keys("我手机 13812345678，备用 +86 138 1234 5678")
    phones = [k for k in keys if k.pattern_name == "phone"]
    assert len(phones) >= 1


def test_extract_phone_400_vanity() -> None:
    keys = extract_keys("客服 400-800-1234")
    phones = [k for k in keys if k.pattern_name == "phone"]
    assert any("400" in p.text for p in phones)


def test_extract_phone_skips_short_digit_blob() -> None:
    """5-6 digit blobs are usually IDs / prices, not phones."""
    keys = extract_keys("订单号 12345")
    phones = [k for k in keys if k.pattern_name == "phone"]
    assert phones == []


def test_extract_social_wechat() -> None:
    keys = extract_keys("微信号 alice_2024")
    socials = [k for k in keys if k.pattern_name == "social"]
    assert len(socials) == 1


def test_extract_social_qq() -> None:
    keys = extract_keys("QQ 1234567")
    socials = [k for k in keys if k.pattern_name == "social"]
    assert len(socials) == 1


def test_extract_social_github() -> None:
    keys = extract_keys("github acme-engineering 上有源码")
    socials = [k for k in keys if k.pattern_name == "social"]
    assert len(socials) >= 1


def test_extract_social_at_handle() -> None:
    keys = extract_keys("找 @alice_dev 帮看")
    socials = [k for k in keys if k.pattern_name == "social"]
    assert len(socials) >= 1


def test_extract_windows_path() -> None:
    keys = extract_keys("代码在 C:\\Users\\me\\proj\\src\\main.py")
    paths = [k for k in keys if k.pattern_name == "path"]
    assert any("main.py" in p.text for p in paths)


def test_extract_posix_path() -> None:
    keys = extract_keys("配置在 /etc/nginx/nginx.conf 改一下")
    paths = [k for k in keys if k.pattern_name == "path"]
    assert any("/etc/nginx" in p.text for p in paths)


def test_extract_home_path() -> None:
    keys = extract_keys("放 ~/Desktop/notes.md 里")
    paths = [k for k in keys if k.pattern_name == "path"]
    assert any("~/Desktop" in p.text for p in paths)


def test_extract_tech_stack() -> None:
    keys = extract_keys("我用 Python 写后端，栈是 FastAPI + Postgres")
    stacks = [k for k in keys if k.pattern_name == "stack"]
    assert len(stacks) >= 1


def test_extract_deadline_chinese() -> None:
    keys = extract_keys("截止日期 6月底前，DDL 双11")
    dls = [k for k in keys if k.pattern_name == "deadline"]
    assert len(dls) >= 1


def test_extract_deadline_english() -> None:
    keys = extract_keys("submit by Friday or by next Monday")
    dls = [k for k in keys if k.pattern_name == "deadline"]
    assert len(dls) >= 1


def test_extract_datetime_iso() -> None:
    keys = extract_keys("会议 2026-05-15 10点")
    dts = [k for k in keys if k.pattern_name == "datetime"]
    assert any("2026-05-15" in d.text for d in dts)


def test_extract_datetime_chinese_relative() -> None:
    keys = extract_keys("下周一上午 10 点开会，每周三复盘")
    dts = [k for k in keys if k.pattern_name == "datetime"]
    assert len(dts) >= 2


def test_datetime_skips_bare_today() -> None:
    """Bare '今天' / '明天' is too noisy as a fact — skip."""
    keys = extract_keys("今天天气真好")
    dts = [k for k in keys if k.pattern_name == "datetime"]
    assert dts == []


def test_extract_money_chinese() -> None:
    keys = extract_keys("预算 5 万，报价 12.5 万 RMB")
    monies = [k for k in keys if k.pattern_name == "money"]
    assert len(monies) >= 1


def test_extract_money_usd() -> None:
    keys = extract_keys("budget $500K total")
    monies = [k for k in keys if k.pattern_name == "money"]
    assert len(monies) >= 1


def test_extract_relationship_family() -> None:
    keys = extract_keys("我老婆叫小红，我儿子小明今年 5 岁")
    rels = [k for k in keys if k.pattern_name == "relationship"]
    assert len(rels) >= 2


def test_extract_relationship_work() -> None:
    keys = extract_keys("我同事 Alice 负责前端，我老板叫 Bob")
    rels = [k for k in keys if k.pattern_name == "relationship"]
    assert len(rels) >= 2


def test_extract_constraint_chinese() -> None:
    keys = extract_keys("必须用 PowerShell，永远别用 bash")
    cs = [k for k in keys if k.pattern_name == "constraint"]
    assert len(cs) >= 1


def test_extract_constraint_english() -> None:
    keys = extract_keys("must use HTTPS, never store passwords in plain text")
    cs = [k for k in keys if k.pattern_name == "constraint"]
    assert len(cs) >= 1


def test_extract_org() -> None:
    keys = extract_keys("公司名 Acme Corp，项目叫 NimbusBot")
    orgs = [k for k in keys if k.pattern_name == "org"]
    assert len(orgs) >= 1


# ── Mega-realistic message — multiple categories ─────────────────


def test_mega_realistic_message() -> None:
    """Stress-test a message that covers half a dozen categories."""
    msg = (
        "我是干陪玩店的，公司名 NimbusGames，"
        "网站 https://pw310.wxselling.com，账号 admin / admin888，"
        "客服电话 400-800-1234，邮箱 contact@nimbus.cn，"
        "微信号 nimbus_cs，我老婆叫小红负责运营，"
        "用 Python + FastAPI 写后端，预算 5 万，"
        "截止日期 6月底前，每周三复盘。"
        "目标是月流水破 10 万。记住这些。"
    )
    keys = extract_keys(msg)
    patterns_hit = {k.pattern_name for k in keys}
    expected = {
        "url", "credential", "cred_pair", "goal", "email", "phone",
        "social", "stack", "deadline", "datetime", "money",
        "relationship", "identity", "remember_directive", "org",
    }
    # At least 12 distinct patterns from the expected set should fire.
    actual = patterns_hit & expected
    assert len(actual) >= 12, (
        f"only matched {len(actual)} of {len(expected)} expected patterns: "
        f"{sorted(actual)}"
    )
