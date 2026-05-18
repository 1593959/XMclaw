"""Wave-32+ SAME_TOPIC bridge — entity-token + cross-kind tests.

Pin the new behavior the user explicitly asked for:

  * Facts that share a URL / admin / identifier link even across kinds
  * CJK bi-gram bridge: "陪玩店账号" links to "陪玩店地址" via "陪玩店"
  * Stopwords don't bridge ("我们" / "可以" alone won't link facts)
  * ASCII length floor: short tokens like "id" / "is" don't bridge
"""
from __future__ import annotations

from xmclaw.memory.v2.service import _extract_entity_tokens


def test_url_extracted_with_components() -> None:
    """A URL produces multiple distinctive tokens: the full URL,
    the domain, sub-identifier parts. Each is a bridge candidate."""
    toks = _extract_entity_tokens("网址: https://pw310.wxselling.com/login")
    assert "https://pw310.wxselling.com/login" in toks
    assert "pw310" in toks
    assert "wxselling" in toks
    # CJK bi-gram includes "网址".
    assert "网址" in toks


def test_credential_facts_bridge_on_shared_admin() -> None:
    """The two facts the user screenshotted as a connected cluster:
    "账号是admin" and "陪玩店账号为admin" should overlap on the
    ASCII identifier 'admin' AND on the CJK bigram '账号'."""
    t_creds = _extract_entity_tokens("凭据: 账号是admin")
    t_peiwan = _extract_entity_tokens("陪玩店账号为admin")
    overlap = t_creds & t_peiwan
    assert "admin" in overlap
    assert "账号" in overlap


def test_url_vs_peiwan_address_share_url_tokens() -> None:
    """The 2nd-screenshot case — two clusters that both reference
    the same URL should bridge via URL tokens regardless of which
    fact 'kind' each one was extracted as."""
    t_url = _extract_entity_tokens("网址: https://pw310.wxselling.com/login")
    t_addr = _extract_entity_tokens(
        "陪玩店网站地址为 https://pw310.wxselling.com"
    )
    overlap = t_url & t_addr
    assert "pw310" in overlap
    assert "wxselling" in overlap


def test_cjk_bigram_bridge_across_facts() -> None:
    """Two facts that don't share any English token but mention
    the same CJK noun should still bridge — the bi-gram window
    catches "陪玩店" inside longer phrases like "陪玩店账号" /
    "陪玩店地址"."""
    a = _extract_entity_tokens("陪玩店账号为 ROOT")
    b = _extract_entity_tokens("陪玩店地址在朝阳区")
    # Bi-gram emission means "陪玩店" appears as both "陪玩" + "玩店".
    # Either bigram in the intersection is enough to bridge the facts.
    overlap = a & b
    assert "陪玩" in overlap
    assert "玩店" in overlap


def test_stopwords_not_emitted() -> None:
    """Common pronouns / function words can't bridge facts —
    otherwise every fact mentioning "我们" would cross-link."""
    toks = _extract_entity_tokens("我们 可以 这个 那个 应该")
    assert "我们" not in toks
    assert "可以" not in toks
    # ASCII stopwords similarly filtered.
    assert "this" not in _extract_entity_tokens("this and that")
    assert "the" not in _extract_entity_tokens("the foo bar")


def test_short_ascii_tokens_filtered() -> None:
    """ASCII tokens under 4 chars are too noisy — exclude. Otherwise
    fact pairs sharing "id" / "is" / "go" would over-link."""
    toks = _extract_entity_tokens("set id to 7 go run")
    assert "id" not in toks
    assert "go" not in toks
    # Length-4 distinctive ones stay.
    assert "admin" not in toks  # not in this sentence
    toks2 = _extract_entity_tokens("user admin password")
    assert "admin" in toks2
    assert "password" in toks2


def test_empty_input_returns_empty_set() -> None:
    assert _extract_entity_tokens("") == set()
    assert _extract_entity_tokens(None or "") == set()


def test_user_screenshot_orphan_case_bridges() -> None:
    """The exact pair from the screenshot — a URL fact and a
    plain-Chinese "目标网站..." fact — currently does NOT bridge via
    tokens (no shared bi-gram between "网址" and "目标网站..."). Pin
    this as the known limit so a future improvement (synonym lex)
    can flip the assertion when implemented.

    The widened VEC scan path (drop same-kind + limit=10) is the
    fallback for this case — covered by integration tests elsewhere.
    """
    t_url = _extract_entity_tokens(
        "网址: https://pw310.wxselling.com/login"
    )
    t_floating = _extract_entity_tokens("目标网站无需验证码即可访问")
    # No bi-gram intersection: "网址" (one bigram) vs bigrams of
    # the floating text don't overlap.
    assert (t_url & t_floating) == set()
