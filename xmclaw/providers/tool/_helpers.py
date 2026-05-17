"""Bullet / dedup / persona-cap / web-parse helpers used by built-in tools.

Lifted out of ``builtin.py`` (B-324). Three concerns end up here
because they share clients across the file:

* **Bullet handling** — ``_bullet_core`` / ``_bullet_token_set`` /
  ``_is_fuzzy_duplicate`` / ``_append_under_section``. Used by
  ``remember`` / ``learn_about_user`` / ``memory_pin`` /
  ``update_persona`` write paths *and* by external callers
  (``post_sampling_hooks.py`` re-imports them through ``builtin``).
* **Persona-file caps** — ``PERSONA_CHAR_CAPS`` /
  ``enforce_char_cap`` / ``collapse_existing_duplicates`` /
  ``_PERSONA_BASENAMES_LOOKUP``. Drives LRU eviction by date prefix
  on MEMORY.md / USER.md / AGENTS.md / TOOLS.md.
* **Web search parsing** — ``_parse_ddg_html``. Hand-rolled DDG-HTML
  scraper for ``web_search``.
* **Tool error envelope** — ``_fail`` builds a uniform ``ToolResult``
  with measured latency.

``builtin.py`` re-imports each of these into its own namespace at
load time so external imports like::

    from xmclaw.providers.tool.builtin import (
        PERSONA_CHAR_CAPS, _append_under_section, enforce_char_cap,
    )

keep working without callers having to learn the new path.
"""
from __future__ import annotations

import re
import time

from xmclaw.core.ir import ToolCall, ToolResult


# B-25 (Hermes parity): legacy on-disk file basenames the agent / Web
# UI may pass to the ``update_persona`` tool in any case. Used for
# canonical resolution. Defined here (not in builtin.py) so the same
# name table is available to the helpers below + any module that
# imports them.
_PERSONA_BASENAMES_LOOKUP: dict[str, str] = {}
for _b in (
    "AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md",
    "TOOLS.md", "BOOTSTRAP.md", "MEMORY.md",
):
    _PERSONA_BASENAMES_LOOKUP[_b.lower()] = _b
    _PERSONA_BASENAMES_LOOKUP[_b.lower().removesuffix(".md")] = _b


# ── helpers ───────────────────────────────────────────────────────────

_BULLET_DATE_RE = re.compile(
    r"^\s*-\s*\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?(?:\s+[A-Z]{2,5})?\s*[:：]?\s*"
)
# B-183: bare date prefix (no leading "-") for the SECOND strip pass —
# legacy "- 2026-05-02: 2026-05-02: ..." rows produced by pre-B-179
# extractors land with the inner date naked after the first strip.
_BARE_DATE_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?(?:\s+[A-Z]{2,5})?\s*[:：]?\s*"
)


def _bullet_core(line: str) -> str:
    """Extract the meat of a bullet for dedup comparison.

    Strips ``- YYYY-MM-DD: `` (and variants with time / TZ) plus
    surrounding whitespace, then lowercases and collapses internal
    whitespace. Two bullets compare equal iff they say the same thing
    regardless of when they were written.

    B-183: also strips a bare second date prefix to handle legacy
    "- 2026-05-02: 2026-05-02: real content" entries that appeared
    on disk before B-179 fixed the LLM-extracted leading-date bug.
    """
    cleaned = _BULLET_DATE_RE.sub("", line.strip())
    # Some entries got nested ``YYYY-MM-DD: YYYY-MM-DD: ...`` from
    # earlier dedup-less runs — strip a bare second date prefix too
    # (no leading "-" since the first pass already removed the bullet
    # marker).
    cleaned = _BARE_DATE_RE.sub("", cleaned).strip()
    # Normalise punctuation/whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).lower()
    # Strip trailing punctuation that doesn't change semantics.
    return cleaned.rstrip(".。,，!！?？")


# B-183 fuzzy dedup: when the LLM paraphrases an existing fact, the
# strict ``_bullet_core`` exact match doesn't catch it (real example
# from MEMORY.md: "events.db tool_invocation_started 的 name 在
# payload JSON" vs "events.db tool name 存在 payload JSON 里" — same
# fact, prose rewritten, identical SQL). Token-set Jaccard catches
# these without needing an LLM call.
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
# Common Chinese + English glue words that appear in many bullets but
# carry no signal — drop from the token set so two prose styles with
# different fillers can still match on content tokens.
_BULLET_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "of", "in", "to", "for", "on", "at", "by", "with", "from",
    "and", "or", "but", "if", "as", "that", "this", "these",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "it", "its", "into", "than",
    "的", "了", "和", "在", "是", "也", "要", "对", "把", "让",
    "可以", "应该", "已经", "需要", "我们", "他们", "如果",
    "或者", "不是", "里", "上", "下", "时", "中", "就", "都",
    "又", "再", "其他", "这个", "那个", "什么", "哪个",
})
# Minimum token-set Jaccard overlap to consider two bullets duplicates.
# 0.7 caught the events.db SQL paraphrase case in real-data testing
# without false-positiving on bullets that share a common technical
# topic but say different things.
_FUZZY_DUP_JACCARD = 0.7


def _bullet_token_set(line: str) -> frozenset[str]:
    """Tokenise a bullet's core text into a stopword-stripped set,
    suitable for Jaccard comparison against another bullet."""
    core = _bullet_core(line)
    if not core:
        return frozenset()
    tokens: set[str] = set()
    for tok in _TOKEN_RE.findall(core):
        tok = tok.lower()
        if len(tok) <= 1:
            continue  # single-char tokens are too noisy
        if tok in _BULLET_STOPWORDS:
            continue
        tokens.add(tok)
    return frozenset(tokens)


def _is_fuzzy_duplicate(
    incoming: frozenset[str], existing: frozenset[str],
    *, threshold: float = _FUZZY_DUP_JACCARD,
) -> bool:
    """Jaccard(incoming, existing) >= threshold and both sets non-trivial.

    Trivial-set guard: bullets with fewer than 4 unique content tokens
    are too small to make Jaccard meaningful — skip the fuzzy check
    for them and rely on exact match only.
    """
    if len(incoming) < 4 or len(existing) < 4:
        return False
    intersection = len(incoming & existing)
    union = len(incoming | existing)
    if union == 0:
        return False
    return (intersection / union) >= threshold


def _append_under_section(
    existing: str, *, section_header: str, bullet: str, placeholder_title: str,
) -> str:
    """Append ``bullet`` under ``section_header`` (a line like ``## Foo``).

    Behavior:
    * If the file is empty, write a stub: ``# placeholder_title``,
      blank line, then the section + bullet.
    * If the section exists, locate it and append the bullet at the end
      of that section (just before the next ``## `` heading or EOF).
    * If the section is missing, append a new ``## section`` block at
      the bottom of the file with the bullet under it.

    Dedup (B-23): if a semantically-identical bullet already exists
    anywhere in the file (date stripped + whitespace normalised), the
    write is a no-op. Without this, every reflection adds the same
    insight again — MEMORY.md / USER.md grow unboundedly with
    duplicates.

    Strips a trailing newline from ``existing`` first so we don't accumulate
    blank lines on every call.
    """
    if not existing.strip():
        # Brand-new file. Plant a top heading so the file reads naturally.
        return (
            f"# {placeholder_title}\n\n"
            f"{section_header}\n\n"
            f"{bullet}\n"
        )

    body = existing.rstrip("\n")
    lines = body.split("\n")

    # Dedup: skip the write entirely if the same fact (after date strip
    # + normalisation) already appears in the file. We compare against
    # ALL bullets, not just the target section, because the agent
    # sometimes files things under different headings on different days.
    # B-183: also catch fuzzy duplicates — paraphrased restatements with
    # high token-set Jaccard overlap. The strict exact-match path runs
    # first (fast); fuzzy is the fallback for prose-rewritten facts.
    incoming_core = _bullet_core(bullet)
    incoming_tokens = _bullet_token_set(bullet)
    if incoming_core:
        for ln in lines:
            stripped = ln.strip()
            if not stripped or not stripped.startswith("-"):
                continue
            existing_core = _bullet_core(stripped)
            if existing_core == incoming_core:
                # Already there — exact match, return unchanged.
                return existing if existing.endswith("\n") else existing + "\n"
            if _is_fuzzy_duplicate(
                incoming_tokens, _bullet_token_set(stripped),
            ):
                # Paraphrased restatement of an existing fact — skip
                # silently. B-183 caught real cases like the same SQL
                # query rewritten with different prose lead-in.
                return existing if existing.endswith("\n") else existing + "\n"

    # Locate the section.
    try:
        sec_idx = next(
            i for i, ln in enumerate(lines)
            if ln.strip() == section_header.strip()
        )
    except StopIteration:
        # Section missing → append a new block.
        return body + "\n\n" + section_header + "\n\n" + bullet + "\n"

    # Find end of this section: either the next ``## `` line or EOF.
    end_idx = len(lines)
    for j in range(sec_idx + 1, len(lines)):
        s = lines[j].lstrip()
        if s.startswith("## ") or s.startswith("# "):
            end_idx = j
            break

    # Trim trailing blank lines inside the section before our insert.
    insert_at = end_idx
    while insert_at > sec_idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1

    new_lines = (
        lines[:insert_at]
        + [bullet]
        + lines[insert_at:]
    )
    return "\n".join(new_lines) + "\n"


# B-25 (Hermes parity): char-level cap on persona files. The
# defaults follow Hermes' MemoryStore (MEMORY.md=2200, USER.md=1375)
# — bigger than that is a sign of bloat, not insight density. Eviction
# is LRU by ENTRY (lines starting with "-"): drop the oldest bullets
# in the largest section first, keep the file's frontmatter + section
# headers + non-bullet prose intact.
PERSONA_CHAR_CAPS: dict[str, int] = {
    "MEMORY.md": 2200,
    "USER.md":   1375,
    # B-168: AGENTS.md / TOOLS.md gain auto-extracted lesson buckets,
    # so they need a cap too — same heuristic (LRU evict oldest dated
    # bullets when over budget). Slightly bigger than USER because a
    # workflow lesson tends to be one paragraph not one phrase.
    "AGENTS.md": 2000,
    # Wave-27 fix-LAT13a: TOOLS.md cap is BIG because the auto-
    # rendered tool list (XMC-AUTO-TOOLS marker block) needs to fit.
    # 130 registered tools × ~100 chars each = ~13K chars baseline;
    # the cap is set higher to absorb that + a few KB of manual
    # guidance and dated auto-extracted lessons. enforce_char_cap
    # only evicts dated bullets, so the marker block (unprefixed
    # bullets) is structurally safe.
    "TOOLS.md":  18000,
    # Wave-27 fix-LAT13 (2026-05-17): SOUL.md / LEARNING.md gained
    # ``## Auto-extracted`` sections (B-303 routes ``values`` →
    # SOUL.md and ``rules`` → LEARNING.md) and BOTH were uncapped.
    # Real-data measurement: a single session grew SOUL.md auto-
    # extracted to 13393 chars and LEARNING.md to 13076 chars,
    # blowing the system prompt to 37K tokens — every turn. With
    # 130 tool specs (~16K tokens) the prompt overhead alone left
    # only ~200K tokens for messages, and Kimi 256K rejected
    # multi-hop turns. Capping at 4K/6K keeps the most recent
    # bullets (LRU eviction by YYYY-MM-DD prefix) and shrinks
    # system_prompt to ~7K tokens total. Loss is bounded: the
    # underlying L1 facts stay queryable via memory_search.
    "SOUL.md":     4000,
    "LEARNING.md": 6000,
    # IDENTITY.md / BOOTSTRAP.md remain uncapped — user-authored,
    # not auto-appended.
}


def enforce_char_cap(text: str, cap: int) -> str:
    """If ``text`` exceeds ``cap`` chars, drop oldest bullets until
    it fits. Returns possibly-shrunk text. No-op when already small.

    Heuristic for "oldest": bullets sort by the ``YYYY-MM-DD`` prefix
    that ``remember`` / ``learn_about_user`` write — earliest date
    evicts first. Bullets without a date prefix are evicted only when
    everything else is gone.
    """
    if len(text) <= cap:
        return text

    lines = text.split("\n")

    def _bullet_date(ln: str) -> str:
        """Return the YYYY-MM-DD prefix or empty string."""
        m = re.match(r"\s*-\s*(\d{4}-\d{2}-\d{2})", ln)
        return m.group(1) if m else ""

    # Index every bullet line for eviction candidacy. Non-bullet lines
    # (headers, frontmatter, prose) are preserved in place.
    bullet_idx = [
        (i, _bullet_date(ln))
        for i, ln in enumerate(lines)
        if ln.strip().startswith("-")
    ]
    if not bullet_idx:
        return text  # nothing to evict

    # Order bullets oldest-first. Empty date sorts FIRST (evict
    # context-less bullets earliest because we have no temporal info
    # to weigh them).
    bullet_idx.sort(key=lambda x: (x[1] or ""))

    drop_set: set[int] = set()
    out_text = text
    while len(out_text) > cap and bullet_idx:
        drop_idx, _ = bullet_idx.pop(0)
        drop_set.add(drop_idx)
        # Recompute size with evictions applied.
        out_text = "\n".join(
            ln for i, ln in enumerate(lines) if i not in drop_set
        )

    # Strip trailing blank lines that may now form runs.
    out_text = re.sub(r"\n{3,}", "\n\n", out_text).rstrip() + "\n"
    return out_text


def collapse_existing_duplicates(
    existing: str, *, max_bullets_per_section: int = 50,
) -> str:
    """One-shot cleanup: walk an already-bloated MEMORY/USER.md and
    drop bullets that have a duplicate earlier in the file. Keeps
    the *first* occurrence (so the original date stamp survives).

    Used by ``cleanup_persona_duplicates`` on demand — e.g. via a
    REST endpoint or the Memory page UI's "整理" button.
    """
    lines = existing.split("\n")
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("-"):
            core = _bullet_core(stripped)
            if core and core in seen:
                continue
            if core:
                seen.add(core)
        out.append(ln)
    return "\n".join(out)


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _parse_ddg_html(html: str, max_results: int) -> list[dict[str, str]]:
    """Pull the top N results out of DuckDuckGo HTML.

    Hand-rolled parser (no bs4 dependency) because we want zero extra
    deps. The HTML page uses a reasonably stable structure:

        <a class="result__a" href="...">TITLE</a>
        ...
        <a class="result__snippet" ...>SNIPPET</a>

    We look for those two anchors in order and pair them up. Breakage
    is expected occasionally -- when that happens the tool returns
    zero results rather than exploding.
    """
    import html as _html
    import re

    results: list[dict[str, str]] = []
    title_re = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    snippet_re = re.compile(
        r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    titles = title_re.findall(html)
    snippets = snippet_re.findall(html)

    def _clean(s: str) -> str:
        # Strip tags, unescape HTML entities, collapse whitespace.
        s = re.sub(r"<[^>]+>", "", s)
        s = _html.unescape(s)
        return " ".join(s.split())

    def _strip_redirect(u: str) -> str:
        # DDG often wraps URLs as /l/?uddg=...&u=<target>. Try to unwrap.
        if u.startswith("/"):
            try:
                from urllib.parse import parse_qs, urlparse
                p = urlparse(u)
                q = parse_qs(p.query)
                for key in ("uddg", "u"):
                    if key in q:
                        return q[key][0]
            except Exception:  # noqa: BLE001
                pass
        return u

    for i, (href, title_html) in enumerate(titles[:max_results]):
        url = _strip_redirect(_html.unescape(href))
        title = _clean(title_html)
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        if not title:
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
    return results
