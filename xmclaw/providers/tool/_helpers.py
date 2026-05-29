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
# 2026-05-29 cleanup: LEARNING.md was missing pre-v3 — the table
# pre-dated the bucket→file routing's ``rules`` bucket which renders
# to LEARNING.md. Added so ``update_persona`` and the v3 ``memory_get``
# tool agree on the canonical persona file set.
for _b in (
    "AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md",
    "TOOLS.md", "BOOTSTRAP.md", "MEMORY.md", "LEARNING.md",
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


# PERSONA_CHAR_CAPS / enforce_char_cap moved to xmclaw.core.persona.caps
# (2026-05-17) so the core-side v2_renderer can apply them without
# violating the "core cannot import from providers" architectural rule
# enforced by scripts/check_import_direction.py. Re-exported from here
# to keep the legacy import paths
# ``from xmclaw.providers.tool._helpers import PERSONA_CHAR_CAPS, ...``
# and ``from xmclaw.providers.tool.builtin import ...`` working without
# touching ~10 call sites.
from xmclaw.core.persona.caps import (  # noqa: E402,F401 — re-export
    PERSONA_CHAR_CAPS as PERSONA_CHAR_CAPS,
    enforce_char_cap as enforce_char_cap,
)


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


def _fail_with_hint(
    call: ToolCall,
    t0: float,
    summary: str,
    *,
    exc: BaseException | None = None,
    hint: str | None = None,
) -> ToolResult:
    """Epic #27 sweep #16 (2026-05-19): structured-error variant of
    ``_fail`` that includes an actionable hint when the failure has
    a known recovery path.

    Pre-fix many tool exception sites returned raw
    ``f"{type(exc).__name__}: {exc}"`` — accurate but the LLM
    couldn't tell from "PermissionError: ..." whether the fix was
    "try a different path" or "ask the user to run with elevated
    privileges". Hints close that gap: a one-line recovery
    suggestion the LLM can read + act on.

    Format: ``"<summary> | <exc-type>: <exc-msg> | hint: <hint>"``
    — pipe-separated so the agent's prompt-printer can render
    cleanly + downstream graders can split if needed.

    All fields optional except summary; if neither exc nor hint
    given, behaves identically to ``_fail``.
    """
    parts: list[str] = [summary]
    if exc is not None:
        parts.append(f"{type(exc).__name__}: {exc}")
    if hint is not None:
        parts.append(f"hint: {hint}")
    return ToolResult(
        call_id=call.id, ok=False, content=None,
        error=" | ".join(parts),
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


def _parse_bing_html(html: str, max_results: int) -> list[dict[str, str]]:
    """2026-05-28: parser for Bing CN SERP HTML.

    Bing's organic results all live in ``<li class="b_algo">`` blocks.
    Each block has a ``<h2><a href="...">TITLE</a></h2>`` and a
    snippet container — typically ``<div class="b_caption">...<p>SNIPPET</p>``
    or ``<p class="b_lineclamp...">SNIPPET</p>`` depending on the
    Bing UI version. We grab both possibilities; if Bing changes
    the markup we degrade to zero results rather than crashing
    (same posture as ``_parse_ddg_html``).
    """
    import html as _html
    import re

    def _clean(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        s = _html.unescape(s)
        return " ".join(s.split())

    # Match each <li class="b_algo">...</li> block individually so
    # title/snippet stay paired. Bing emits the closing </li> at the
    # next li or the end of the results container — accept either.
    block_re = re.compile(
        r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)(?=<li[^>]*class="[^"]*\bb_algo\b|</ol>|</main>)',
        re.DOTALL,
    )
    title_re = re.compile(
        r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    # Bing puts snippet inside either:
    #   <div class="b_caption">...<p>SNIPPET</p>
    #   <p class="b_lineclamp...">SNIPPET</p>
    snippet_p_re = re.compile(
        r'<p[^>]*class="[^"]*(?:b_lineclamp|b_paractl)[^"]*"[^>]*>(.*?)</p>',
        re.DOTALL,
    )
    snippet_caption_re = re.compile(
        r'<div[^>]*class="[^"]*\bb_caption\b[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>',
        re.DOTALL,
    )

    results: list[dict[str, str]] = []
    for block_html in block_re.findall(html):
        t = title_re.search(block_html)
        if not t:
            continue
        url = _html.unescape(t.group(1))
        title = _clean(t.group(2))
        if not title or not url:
            continue
        snippet = ""
        m = snippet_p_re.search(block_html) or snippet_caption_re.search(block_html)
        if m:
            snippet = _clean(m.group(1))
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results
