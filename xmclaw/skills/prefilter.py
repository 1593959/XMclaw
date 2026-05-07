"""B-238: skill prefilter — narrow ``skill_*`` tool_specs by query relevance.

Problem (real-data 2026-05-07): user installed 404 skills. AgentLoop
hands the entire list to the LLM as tool_specs every turn — that's
~80K tokens of skill descriptions before the user message even
starts. The LLM's tool-selection signal-to-noise drops to zero;
``skill_git-commit`` and ``skill_create-readme`` look the same when
they're items #137 and #208 in a wall of choices. Result: LLM falls
back to raw tools (``bash``, ``file_write``) instead of routing to a
purpose-built skill.

The fix is the same trick Claude Code / Cursor / OpenAI Assistants
all use under the hood: pre-filter the tool list to top-K relevant
items based on the current query, then let the LLM pick from the
shortlist. Exposes the rest only on miss (the LLM can ask
``skill_find-skills`` to discover them).

Algorithm (this version, deliberately cheap):
  * tokenise query + each skill description (Chinese-aware: char-level
    for CJK, word-level for ASCII)
  * score each skill by:
        2 × name-substring overlap   (skill name contains query word)
      + 1 × description-token overlap
      + 0.5 × trigger-keyword match  (if SKILL.md frontmatter listed
                                      explicit triggers)
  * top-K (default 12) skills survive

What this DOESN'T do (yet, by design):
  * embedding-based semantic match (need embedder dep, doable later)
  * LLM-based 2-stage routing (a small auxiliary call to pick top-K)
  * Learning from successful invocations (which skills got called +
    rated by HonestGrader → bump their score for similar future queries)

Empty-query fallback: when the query has no usable tokens (just
"hi", emoji, etc.) we DON'T filter — return the original list so
the LLM has full context for whatever the user is doing.
"""
from __future__ import annotations

import re
from typing import Any

# CJK character classes — tokenised individually because there's no
# whitespace word boundary in Chinese / Japanese / Korean.
_CJK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힯]"
)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]+")


def _tokenize(text: str) -> set[str]:
    """Return the set of normalised tokens in ``text``.

    ASCII: lowercase words ≥ 2 chars (skip single letters, they
    over-match noise like "a" / "I" / "x").
    CJK: each Han / Hangul / Kana character is its own token.
    """
    if not text:
        return set()
    text_lower = text.lower()
    tokens: set[str] = set()
    for w in _WORD_RE.findall(text_lower):
        if len(w) >= 2:
            tokens.add(w)
    for c in _CJK_RE.findall(text):
        tokens.add(c)
    return tokens


# Stop-words that have near-zero discriminative power. Adding "skill"
# itself is intentional — "find a skill that..." should NOT score
# every skill_* tool 1+ on the literal "skill" overlap.
_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "this", "that", "with",
    "from", "what", "when", "where", "how", "why", "use", "using",
    "skill", "tool", "agent", "please", "can", "would", "could",
    "help", "want", "need", "make", "create", "build", "run",
    # Chinese stopwords (single-char so they tokenise via _CJK_RE)
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "和",
    "也", "都", "就", "要", "不", "有", "没", "去", "来", "下",
    "上", "里", "好", "把", "给", "让", "对", "为", "请", "能",
})


def _score_skill(
    query_tokens: set[str], spec: Any,
) -> float:
    """Score a single skill ``ToolSpec`` against the query token set."""
    if not query_tokens:
        return 0.0
    name = (getattr(spec, "name", "") or "").lower()
    desc = getattr(spec, "description", "") or ""

    # 1. Name-substring overlap (strongest signal: ``skill_git-commit``
    # contains "git" / "commit" tokens directly).
    name_score = 0.0
    for tok in query_tokens:
        if tok in _STOPWORDS or len(tok) < 2:
            continue
        if tok in name:
            name_score += 2.0

    # 2. Description token overlap.
    desc_tokens = _tokenize(desc)
    desc_score = float(len(query_tokens & desc_tokens - _STOPWORDS))

    # 3. Trigger keyword match — ToolSpec's parameters_schema sometimes
    # carries an explicit ``triggers`` field via the SKILL.md
    # frontmatter pipeline; reward exact matches there higher than
    # generic description overlap.
    trigger_score = 0.0
    schema = getattr(spec, "parameters_schema", None) or {}
    triggers = schema.get("x_triggers") if isinstance(schema, dict) else None
    if isinstance(triggers, (list, tuple)):
        for trig in triggers:
            if not isinstance(trig, str):
                continue
            tt = _tokenize(trig)
            trigger_score += 0.5 * len(query_tokens & tt)

    return name_score + desc_score + trigger_score


def select_relevant_skills(
    query: str,
    skill_specs: list[Any],
    *,
    top_k: int = 12,
    min_skills_to_filter: int = 30,
) -> list[Any]:
    """Pick the ``top_k`` most query-relevant skill specs.

    Args:
        query: user's message (latest turn's user content).
        skill_specs: ToolSpec list — only specs whose name starts with
            ``skill_`` are filtered; the rest pass through untouched.
        top_k: how many skill specs to keep. Default 12 — fits in the
            LLM's working window comfortably while still surfacing
            non-obvious matches.
        min_skills_to_filter: don't filter below this skill count
            (small setups don't have the noise problem; let the LLM
            see everything).

    Returns:
        New list with non-skill specs preserved in original position
        + at most ``top_k`` skill specs (in score-descending order),
        followed by zero-score skills truncated.

    On empty / pathological query: returns the input list unchanged.
    """
    # Partition: skills vs non-skills. Non-skills (bash / file_read /
    # web_fetch / etc) ALWAYS pass through — those are the workhorse
    # tools every turn might need.
    skills: list[Any] = []
    others: list[Any] = []
    for spec in skill_specs:
        name = getattr(spec, "name", "") or ""
        if name.startswith("skill_"):
            skills.append(spec)
        else:
            others.append(spec)

    if len(skills) < min_skills_to_filter:
        return list(skill_specs)  # nothing to gain from filtering

    query_tokens = _tokenize(query) - _STOPWORDS
    if not query_tokens:
        return list(skill_specs)  # query has no signal — don't filter blindly

    scored: list[tuple[float, Any]] = [
        (_score_skill(query_tokens, spec), spec) for spec in skills
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    # Drop zero-score skills entirely. With 400 skills and 12-slot
    # budget, anything that didn't match a single token isn't worth
    # the LLM's attention this turn.
    keep_skills = [
        spec for score, spec in scored if score > 0
    ][:top_k]

    # Stable order: non-skills first (they're the always-available
    # workhorses), then the relevant skills.
    return others + keep_skills


__all__ = ["select_relevant_skills"]
