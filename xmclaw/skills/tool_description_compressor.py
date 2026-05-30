"""Tool-description progressive disclosure — P0-3 Phase 1.

Reduces the token footprint of tool definitions sent to the LLM on each
turn by compressing descriptions of low-relevance tools while keeping
core tools fully described.

Background
==========

B-238 (skill prefilter) already drops irrelevant ``skill_*`` tools from
~404 down to ~12. But **non-skill** tools (bash, file_read, browser_*,
etc.) are always sent with their full descriptions. With 30-50 builtin
tools + 12 skills, total tool-definition token count can still reach
20-40K.

This module applies a second layer: within the surviving tool list,
truncate verbose natural-language descriptions for tools that have low
keyword overlap with the user's query. The model still sees every tool
name and every parameter schema — only the prose is shortened.

Design constraints
------------------

1. **Zero LLM calls** — keyword matching only, O(N) per turn.
2. **All tools remain callable** — compression is cosmetic; if the model
   picks a compressed tool it works normally.
3. **Per-turn freshness** — relevance is recomputed every turn so a tool
   compressed in turn N may be full-length in turn N+1 when the query
   shifts.
"""
from __future__ import annotations

import re

from xmclaw.core.ir.toolcall import ToolSpec


# Tools that are ALWAYS kept at full description — they're the universal
# workhorses and should never be compressed.
_CORE_TOOLS: frozenset[str] = frozenset({
    "bash",
    "file_read",
    "file_write",
    "list_dir",
    "web_search",
    "web_fetch",
    "think",
    "ask_user_question",
    "memory_search",
    "todo_write",
})

# Stopwords to exclude from keyword extraction.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "shall",
    "can", "need", "dare", "ought", "used", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into",
    "through", "during", "before", "after", "above", "below",
    "between", "under", "again", "further", "then", "once", "here",
    "there", "when", "where", "why", "how", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "just", "and", "but", "if", "or", "because", "until",
    "while", "this", "that", "these", "those", "i", "me", "my",
    "myself", "we", "our", "you", "your", "he", "him", "his",
    "she", "her", "it", "its", "they", "them", "their", "what",
    "which", "who", "whom", "whose", "s", "t", "don", "doesn",
    "didn", "wasn", "weren", "haven", "hasn", "hadn", "won",
    "wouldn", "couldn", "shouldn", "isn", "aren", "ain", "ll",
    "re", "ve", "d", "m", "o", "ma", "y", "ain", "yours",
    "yourself", "yourselves", "himself", "herself", "itself",
    "themselves", "what", "which", "who", "whom", "this", "that",
    "these", "those", "am", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "having", "do", "does",
    "did", "doing", "a", "an", "the", "and", "but", "if", "or",
    "because", "as", "until", "while", "of", "at", "by", "for",
    "with", "about", "against", "between", "into", "through",
    "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "in", "out", "on", "off", "over", "under",
    "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "can",
    "will", "just", "should", "now",
    # Chinese stopwords
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
    "个", "为", "什么", "们", "来", "能", "把", "还", "可以",
    "让", "给", "请", "用", "怎么", "还是", "需要", "想", "做",
    "一下", "一些", "如果", "然后", "但是", "因为", "所以",
})

# Regex for token extraction: alnum runs + CJK characters.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")

# Description truncation thresholds.
_FULL_DESC_MIN_OVERLAP = 2
_TRUNCATED_DESC_MAX_CHARS = 120
_MINIMAL_DESC_MAX_CHARS = 60


def _extract_tokens(text: str) -> set[str]:
    """Extract searchable tokens from a string."""
    tokens: set[str] = set()
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0)
        if tok not in _STOPWORDS and len(tok) > 1:
            tokens.add(tok)
    return tokens


def _relevance_score(tool: ToolSpec, query_tokens: set[str]) -> int:
    """Count overlapping tokens between tool description/name and query."""
    tool_text = f"{tool.name or ''} {tool.description or ''}"
    tool_tokens = _extract_tokens(tool_text)
    return len(tool_tokens & query_tokens)


def _compress_description(desc: str, max_chars: int) -> str:
    """Truncate description to first sentence or max_chars."""
    if not desc:
        return desc
    if len(desc) <= max_chars:
        return desc
    # Try to find the first sentence end.
    for end in (". ", "。", "\n\n", "\n"):
        idx = desc.find(end)
        if 10 <= idx <= max_chars:
            return desc[: idx + len(end)].rstrip()
    return desc[:max_chars].rstrip() + "..."


def compress_tool_descriptions(
    tool_specs: list[ToolSpec],
    user_message: str,
) -> list[ToolSpec]:
    """Return a copy of ``tool_specs`` with descriptions compressed.

    Compression levels:
      * Core tools → never compressed.
      * High relevance (overlap >= 2) → full description.
      * Medium relevance (overlap == 1) → truncated to first sentence
        or 120 chars.
      * Low relevance (overlap == 0) → one-liner + schema only (60 chars).

    The returned list preserves order. Parameter schemas are untouched.
    """
    if not tool_specs or not user_message:
        return list(tool_specs)

    query_tokens = _extract_tokens(user_message)
    if not query_tokens:
        return list(tool_specs)

    result: list[ToolSpec] = []
    for spec in tool_specs:
        name = spec.name or ""
        if name in _CORE_TOOLS:
            result.append(spec)
            continue

        score = _relevance_score(spec, query_tokens)
        if score >= _FULL_DESC_MIN_OVERLAP:
            result.append(spec)
            continue

        # Compress description.
        original_desc = spec.description or ""
        if score == 1:
            new_desc = _compress_description(original_desc, _TRUNCATED_DESC_MAX_CHARS)
        else:
            new_desc = _compress_description(original_desc, _MINIMAL_DESC_MAX_CHARS)

        if new_desc == original_desc:
            result.append(spec)
        else:
            result.append(ToolSpec(
                name=spec.name,
                description=new_desc,
                parameters_schema=spec.parameters_schema,
                read_only=getattr(spec, "read_only", False),
            ))

    return result


__all__ = ["compress_tool_descriptions"]
