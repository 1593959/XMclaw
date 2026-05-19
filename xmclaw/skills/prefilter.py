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

import fnmatch
import re
from typing import Any, Iterable

# CJK character classes — tokenised individually because there's no
# whitespace word boundary in Chinese / Japanese / Korean.
_CJK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힯]"
)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]+")


# Epic #27 G-05 (2026-05-19): file-op tool names whose ``path`` arg
# feeds the recent-paths window the prefilter consults for conditional
# skill activation. Keep this tight — bash isn't here on purpose
# because its ``command`` is too free-form to reliably extract a path
# from, and false-positive paths poison the conditional gate.
_FS_TOOL_NAMES = frozenset({
    "file_read",
    "file_write",
    "list_dir",
    "file_search",
    "file_glob",
    "file_grep",
    "file_edit",
})


def _norm_path(p: str) -> str:
    """Normalise a path string for matching.

    fnmatch is case-sensitive on Posix and case-insensitive on Windows
    by virtue of how the path was written; we coerce to lowercase +
    forward-slash so a Windows ``C:\\Users\\me\\src\\app.tsx`` glob-
    matches ``src/**/*.tsx`` exactly like a Posix path would.
    """
    return p.replace("\\", "/").lower().strip()


def _path_matches_any(path: str, globs: Iterable[str]) -> bool:
    """True iff ``path`` matches any of ``globs``.

    Accepts ``**`` cross-segment matching (translated to ``*`` for the
    fnmatch engine, which doesn't natively grok recursive globs).
    Matches against both the full normalised path and its basename so
    a manifest ``paths: ["package.json"]`` (no glob, just a name)
    still fires for ``/path/to/repo/package.json``.
    """
    norm = _norm_path(path)
    base = norm.rsplit("/", 1)[-1]
    for g in globs:
        if not isinstance(g, str) or not g.strip():
            continue
        gnorm = _norm_path(g)
        # ``**/`` and ``/**`` cross-segment globs collapse to plain
        # ``*`` for fnmatch since fnmatch's ``*`` already crosses
        # segments (unlike pathlib). This is intentionally a fuzzy
        # match — false negatives hurt more than false positives for
        # a discovery aid.
        flat = gnorm.replace("**/", "").replace("/**", "")
        if fnmatch.fnmatchcase(norm, flat) or fnmatch.fnmatchcase(base, flat):
            return True
        # Bare-name globs like ``package.json`` apply to any segment.
        if "/" not in gnorm and fnmatch.fnmatchcase(base, gnorm):
            return True
    return False


def extract_recent_paths(
    messages: Any, *, lookback: int = 8, max_paths: int = 20,
) -> list[str]:
    """Walk the conversation messages backwards to harvest recent
    file-op tool-call paths.

    Used by the agent loop to feed ``select_relevant_skills``'s
    ``active_paths`` gate. ``lookback`` caps how many tool-result
    messages we scan (newer first); ``max_paths`` caps the returned
    list. Returns paths in newest-first order, de-duped. Never raises.

    The walker reads two shapes:
      * tool_call messages with ``tool_calls=[{name, args:{path:...}}]``
      * tool_result messages with ``tool_call`` + matching tool_call_id

    Both are emitted by ``AgentLoop`` when it stamps tool invocations
    onto the running messages list. Bash commands and skill_<id>
    invocations are deliberately ignored — their args don't fit a
    single-path model.
    """
    if not messages:
        return []
    out: list[str] = []
    seen: set[str] = set()
    scanned = 0
    for msg in reversed(list(messages)):
        if scanned >= lookback:
            break
        # Pull tool_calls list either off ``msg.tool_calls`` or
        # ``msg["tool_calls"]`` — covers both dataclass + dict shapes.
        tool_calls = (
            getattr(msg, "tool_calls", None)
            if not isinstance(msg, dict)
            else msg.get("tool_calls")
        )
        if not tool_calls:
            continue
        scanned += 1
        for tc in tool_calls:
            name = (
                tc.get("name") if isinstance(tc, dict)
                else getattr(tc, "name", None)
            )
            if name not in _FS_TOOL_NAMES:
                continue
            args = (
                tc.get("args") if isinstance(tc, dict)
                else getattr(tc, "args", None)
            )
            if not isinstance(args, dict):
                continue
            for key in ("path", "file_path", "dir", "directory", "pattern"):
                v = args.get(key)
                if isinstance(v, str) and v.strip():
                    norm = v.strip()
                    if norm in seen:
                        continue
                    seen.add(norm)
                    out.append(norm)
                    if len(out) >= max_paths:
                        return out
    return out


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
    query_tokens: set[str],
    spec: Any,
    context_tokens: set[str] | None = None,
) -> float:
    """Score a single skill ``ToolSpec`` against the query token set.

    Jarvisification Phase 5: when ``context_tokens`` is provided (from
    active goals / attention focus), skills that match the cognitive
    context receive a boost even if the current user message doesn't
    mention them directly.  This prevents "I was working on X, then
    asked a vague follow-up" from dropping the X-relevant skill.
    """
    if not query_tokens and not context_tokens:
        return 0.0
    name = (getattr(spec, "name", "") or "").lower()
    desc = getattr(spec, "description", "") or ""

    tokens = query_tokens or set()
    ctx = context_tokens or set()

    # 1. Name-substring overlap (strongest signal).
    name_score = 0.0
    for tok in tokens:
        if tok in _STOPWORDS or len(tok) < 2:
            continue
        if tok in name:
            name_score += 2.0
    # Context boost: weaker but keeps relevant skills alive across
    # vague follow-ups.
    for tok in ctx:
        if tok in _STOPWORDS or len(tok) < 2:
            continue
        if tok in name:
            name_score += 0.8

    # 2. Description token overlap.
    desc_tokens = _tokenize(desc)
    desc_score = float(len(tokens & desc_tokens - _STOPWORDS))
    desc_score += 0.3 * float(len(ctx & desc_tokens - _STOPWORDS))

    # 3. Trigger keyword match.
    trigger_score = 0.0
    schema = getattr(spec, "parameters_schema", None) or {}
    triggers = schema.get("x_triggers") if isinstance(schema, dict) else None
    if isinstance(triggers, (list, tuple)):
        for trig in triggers:
            if not isinstance(trig, str):
                continue
            tt = _tokenize(trig)
            trigger_score += 0.5 * len(tokens & tt)
            trigger_score += 0.2 * len(ctx & tt)

    return name_score + desc_score + trigger_score


def select_relevant_skills(
    query: str,
    skill_specs: list[Any],
    *,
    top_k: int = 12,
    min_skills_to_filter: int = 30,
    cognitive_state: Any | None = None,
    active_paths: list[str] | tuple[str, ...] | None = None,
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
    # B-299: meta-discovery tool is ALWAYS exposed (regardless of
    # token match) so the LLM has an out-of-band path to find skills
    # the prefilter would have dropped. Without this, a CJK query
    # against English skill descriptions returns 0 skill_* tools —
    # the LLM literally can't see they exist.
    from xmclaw.skills.tool_bridge import (
        META_BROWSE_TOOL_NAME,
        META_DIFF_TOOL_NAME,
        META_INSTALL_TOOL_NAME,
        META_PROPOSE_TOOL_NAME,
        META_ROLLBACK_TOOL_NAME,
        META_RUN_TOOL_NAME,
        META_STATUS_TOOL_NAME,
        META_UNINSTALL_TOOL_NAME,
        META_VIEW_TOOL_NAME,
    )
    _ALWAYS_ON_META = frozenset({
        META_BROWSE_TOOL_NAME,
        META_INSTALL_TOOL_NAME,
        META_UNINSTALL_TOOL_NAME,
        # Epic #27 P0 G-01 (2026-05-19): introspection tools always
        # on so the agent can self-diagnose install failures + read
        # skill bodies without prefilter dropping them.
        META_STATUS_TOOL_NAME,
        META_VIEW_TOOL_NAME,
        # Epic #27 G-04 (2026-05-19): progressive-disclosure run
        # dispatcher. ALWAYS exposed because in ``unified`` mode it's
        # the ONLY skill-invocation path; the prefilter must never
        # drop it even on zero-token-overlap queries.
        META_RUN_TOOL_NAME,
        # Epic #27 G-07 (2026-05-19): versioned-edit history. Always
        # on so the recovery path ("rollback the skill I just broke")
        # is always reachable, no matter what the user query was.
        META_DIFF_TOOL_NAME,
        META_ROLLBACK_TOOL_NAME,
        # Epic #27 G-08 (2026-05-19): self-evolving skill_propose.
        # Always on so the agent can author skills regardless of
        # what tokens the user query carried.
        META_PROPOSE_TOOL_NAME,
    })

    # Partition: skills vs non-skills vs the always-on meta-tools.
    # Non-skills (bash / file_read / web_fetch / etc) ALWAYS pass
    # through — those are the workhorse tools every turn might need.
    skills: list[Any] = []
    others: list[Any] = []
    meta: list[Any] = []
    for spec in skill_specs:
        name = getattr(spec, "name", "") or ""
        if name in _ALWAYS_ON_META:
            meta.append(spec)
        elif name.startswith("skill_"):
            skills.append(spec)
        else:
            others.append(spec)

    if len(skills) < min_skills_to_filter:
        return list(skill_specs)  # nothing to gain from filtering

    query_tokens = _tokenize(query) - _STOPWORDS

    # Phase 5: harvest tokens from active goals + attention focus.
    context_tokens: set[str] = set()
    if cognitive_state is not None:
        ctx_parts: list[str] = []
        for g in getattr(cognitive_state, "current_goals", []):
            ctx_parts.append(getattr(g, "text", "") or "")
        for a in getattr(cognitive_state, "attention_focus", []):
            ctx_parts.append(getattr(a, "content", "") or "")
        context_tokens = _tokenize(" ".join(ctx_parts)) - _STOPWORDS

    paths_signal = bool(active_paths)
    if not query_tokens and not context_tokens and not paths_signal:
        return list(skill_specs)  # no signal — don't filter blindly

    scored: list[tuple[float, Any]] = []
    for spec in skills:
        s = _score_skill(query_tokens, spec, context_tokens)
        # Epic #27 G-05 (2026-05-19): conditional activation via the
        # manifest ``paths`` glob list (stamped onto the spec schema
        # under ``x_paths`` by SkillToolProvider._spec_for).
        #
        # - Skill declares ``paths`` AND any active_path matches one:
        #     boost +3.0 — strong signal this skill belongs in the
        #     current file context.
        # - Skill declares ``paths`` AND NONE of the active_paths match:
        #     score = -1.0 (drops out via the > 0 gate below). The
        #     skill's author explicitly opted in to conditional
        #     activation; surfacing it for unrelated files is noise.
        # - Skill doesn't declare ``paths``: untouched (preserves
        #     legacy behaviour for the majority of skills).
        #
        # We deliberately DON'T look at active_paths when no skill has
        # declared paths — token + context score still drive ranking.
        spec_paths = None
        ps = getattr(spec, "parameters_schema", None) or {}
        if isinstance(ps, dict):
            spec_paths = ps.get("x_paths")
        if isinstance(spec_paths, (list, tuple)) and spec_paths:
            if active_paths:
                matched = any(
                    _path_matches_any(p, spec_paths) for p in active_paths
                )
                if matched:
                    s += 3.0
                else:
                    s = -1.0  # explicit gate
            else:
                # active_paths empty but skill is path-conditional —
                # don't penalise (we might be in a turn before any
                # file-op has happened yet); fall through to token
                # score as the only signal.
                pass
        scored.append((s, spec))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Drop zero-score skills entirely. With 400 skills and 12-slot
    # budget, anything that didn't match a single token isn't worth
    # the LLM's attention this turn — but the LLM still has the
    # always-on ``skill_browse`` meta-tool (kept in ``meta`` above)
    # to discover them on demand if it suspects one exists. B-299:
    # before the meta-tool was added, this branch left the LLM with
    # ZERO skill exposure; CJK queries against English-described
    # skills hit this every turn.
    keep_skills = [
        spec for score, spec in scored if score > 0
    ][:top_k]

    # Stable order: non-skills first (workhorses), then the meta
    # discovery tool, then matched skills. Meta sits AFTER the
    # workhorse tools so the LLM doesn't burn attention on it when
    # a strong skill_* match is available — but BEFORE matched
    # skills so it's seen on the way down the list.
    return others + meta + keep_skills


__all__ = ["select_relevant_skills"]
