"""SkillToolProvider — expose registered Skills as first-class tools.

After Epic #24 Phase 1 ripped out the xm-auto-evo SKILL.md prompt
injection path, **this is the only way a skill becomes callable by the
agent**. ``SkillRegistry`` HEAD entries (each one having passed
evidence-gated promote — anti-req #12) get bridged to tools the LLM
picks like any other (``bash`` / ``file_read`` / etc.). Direct text
injection of skill bodies into the system prompt is no longer a path.

This module makes any Skill registered at HEAD show up as a tool named
``skill_<skill_id>``, with a permissive object schema so the LLM can
decide what to pass. Tool invocation routes back to
``registry.get(skill_id).run(SkillInput(args=...))`` — same code path
as direct programmatic use.

Tool naming
-----------

Anthropic / OpenAI tool names must match ``[a-zA-Z0-9_-]{1,64}``. Skill
ids commonly contain ``.`` (``demo.read_and_summarize``) which would be
rejected by the wire schema. We map ``.`` → ``__`` (double underscore
as namespace separator) and prefix with ``skill_`` so collisions with
built-in tools are impossible to accidentally introduce.

  ``demo.read_and_summarize`` → tool name ``skill_demo__read_and_summarize``

The reverse map is built once at construction, so ``invoke`` is O(1).

Layering note
-------------

Lives in ``xmclaw/skills/`` rather than ``xmclaw/providers/tool/`` so
the providers layer doesn't grow a new dependency on the skills
package (see ``xmclaw/providers/tool/AGENTS.md`` §2). Conforms to the
``ToolProvider`` shape structurally — no inheritance — and the daemon
composes it via :class:`CompositeToolProvider` at wiring time.
"""
from __future__ import annotations

import re
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.skills.base import SkillInput
from xmclaw.skills.registry import SkillRegistry, UnknownSkillError

_VALID_NAME = re.compile(r"[^a-zA-Z0-9_-]")

# B-299: meta-tool name for the LLM's on-demand skill discovery loop.
# Stays inside the ``skill_`` namespace so the LLM grasps "skills are
# tools, here is one that finds you more". Special-cased in the
# prefilter (xmclaw/skills/prefilter.py) so it ALWAYS passes through
# even when the query has zero token overlap with anything in the
# registry — that's the whole point: when prefilter would have
# returned 0 skills, this is the LLM's fallback discovery affordance.
META_BROWSE_TOOL_NAME = "skill_browse"


def _to_tool_name(skill_id: str) -> str:
    """Convert a skill_id to a wire-safe tool name.

    Replaces ``.`` with ``__`` to preserve namespace boundaries (the
    LLM sees ``demo.foo`` and ``demo.bar`` as ``skill_demo__foo`` and
    ``skill_demo__bar``, still visibly the same family). Other invalid
    chars get squashed to ``_``.
    """
    safe = skill_id.replace(".", "__")
    safe = _VALID_NAME.sub("_", safe)
    return f"skill_{safe}"[:64]


class SkillToolProvider:
    """Bridges :class:`SkillRegistry` HEAD into a :class:`ToolProvider`.

    The tool list is dynamically rebuilt on every ``list_tools()`` call,
    so a promote/rollback that moves HEAD is reflected on the next turn
    without restarting the agent. ``invoke`` also looks up the current
    HEAD — never a stale snapshot, never a phantom tool the registry no
    longer has.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        description_prefix: str = "Skill: ",
        variant_selector: Any = None,
    ) -> None:
        self._registry = registry
        self._description_prefix = description_prefix
        self._tool_name_cache: dict[str, str] | None = None
        # B-295: opt-in variant selector for UCB1 over (skill_id, version)
        # arms. None → always HEAD (legacy behaviour). When wired, each
        # invocation asks the selector which version to run; stats
        # accumulate via the selector's own GRADER_VERDICT subscription.
        self._variant_selector = variant_selector

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_tools(self) -> list[ToolSpec]:
        specs = []
        # B-299: prepend the meta-discovery tool so the LLM can ask
        # "is there a skill for X?" out-of-band when the prefilter's
        # token-overlap match misses (CJK queries hitting English
        # skill descs is the canonical 0-result case). The prefilter
        # special-cases this name to always pass through.
        specs.append(self._browse_spec())
        for skill_id in self._registry.list_skill_ids():
            spec = self._spec_for(skill_id)
            if spec is not None:
                specs.append(spec)
        return specs

    async def invoke(self, call: ToolCall) -> ToolResult:
        """Resolve ``call.name`` back to a skill_id and run it.

        Errors surface as ``ToolResult(ok=False, ...)`` rather than
        raising — the agent loop expects tool failures to come through
        this channel so it can reason about them, not crash the turn.
        """
        t0 = time.perf_counter()

        # B-299: meta-tool short-circuit. ``skill_browse`` is synthesised
        # in ``list_tools`` and isn't a registry-backed Skill, so route
        # it before the registry lookup that would otherwise return
        # ``unknown skill tool``.
        if call.name == META_BROWSE_TOOL_NAME:
            return self._invoke_browse(call, t0)

        skill_id = self._tool_name_to_skill_id(call.name)
        if skill_id is None:
            return self._error_result(
                call.id, f"unknown skill tool: {call.name!r}", t0
            )

        # B-295: variant selection. If a selector is wired, ask it which
        # version of the skill to run for this turn. Falls back to HEAD
        # when selector is None / picks None / errors. The chosen
        # version is recorded so the agent loop can stamp it onto the
        # tool_invocation_finished payload (so grader's verdict goes
        # to the right (skill_id, version) bucket).
        chosen_version = None
        if self._variant_selector is not None:
            try:
                chosen_version = self._variant_selector.pick_version(skill_id)
            except Exception:  # noqa: BLE001
                chosen_version = None
        try:
            skill = self._registry.get(skill_id, version=chosen_version)
        except UnknownSkillError as exc:
            return self._error_result(
                call.id, f"skill {skill_id!r} not at HEAD: {exc}", t0
            )

        try:
            out = await skill.run(SkillInput(args=dict(call.args or {})))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                call.id, f"{type(exc).__name__}: {exc}", t0
            )

        # B-295: surface the chosen version in metadata so agent_loop's
        # GRADER_VERDICT publisher attributes the score to the right
        # arm. Effective version is what registry.get actually returned
        # — fall back to active_version() so legacy callers without a
        # selector still get a real version (vs 0 which collapses every
        # variant onto one bucket).
        effective_version = chosen_version
        if effective_version is None:
            try:
                effective_version = self._registry.active_version(skill_id)
            except Exception:  # noqa: BLE001
                effective_version = None
        result_metadata: dict[str, Any] = {}
        if effective_version is not None:
            result_metadata["skill_version"] = int(effective_version)
            result_metadata["skill_id"] = skill_id
        return ToolResult(
            call_id=call.id,
            ok=bool(out.ok),
            content=out.result,
            error=None if out.ok else _coerce_error(out.result),
            latency_ms=self._elapsed_ms(t0),
            side_effects=tuple(out.side_effects or ()),
            metadata=result_metadata,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ── B-299 meta-discovery ────────────────────────────────────────

    def _browse_spec(self) -> ToolSpec:
        """ToolSpec for the always-exposed ``skill_browse`` meta-tool.

        Description is short on purpose — the LLM only needs to know
        WHEN to call it, not how. Argument schema is one ``query``
        string + optional ``top_k``. The tool always returns a JSON
        list ``[{id, version, description, score}, ...]`` so the
        next-turn LLM can read it like any other tool result.
        """
        skill_count = 0
        try:
            skill_count = len(self._registry.list_skill_ids())
        except Exception:  # noqa: BLE001
            skill_count = 0
        description = (
            f"Discover skills available to you. {skill_count} skill(s) are "
            "registered locally; only the most query-relevant ~12 are "
            "shown to you each turn (via a token-overlap prefilter "
            "that DROPS to zero on CJK queries against English skill "
            "descriptions, or any time keyword overlap is weak). "
            "When you suspect a specialised skill might exist for the "
            "user's intent and you don't see one in your current tool "
            "list, call this BEFORE falling back to bash / web_search / "
            "file_*. Returns id + description + score for the top "
            "matches; on a follow-up turn the matched ``skill_<id>`` "
            "tool will be in your tool list and you can invoke it "
            "directly. Free, fast, no side effects."
        )
        return ToolSpec(
            name=META_BROWSE_TOOL_NAME,
            description=description,
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Plain-language description of what you "
                            "want a skill to do. Multilingual OK; "
                            "the matcher handles CJK + ASCII tokens "
                            "and falls back to substring on the "
                            "concatenated id+description corpus."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "Max matches to return (default 8, hard "
                            "cap at 25). Keep small — wider lists "
                            "burn your context for nothing."
                        ),
                        "minimum": 1,
                        "maximum": 25,
                    },
                },
            },
        )

    def _invoke_browse(self, call: ToolCall, t0: float) -> ToolResult:
        """Synchronous handler — does an in-memory scan of the registry,
        no I/O. Returns a JSON-serialisable list the LLM reads.
        """
        args = dict(call.args or {})
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return self._error_result(
                call.id,
                "skill_browse requires a non-empty 'query' string",
                t0,
            )
        try:
            top_k = int(args.get("top_k", 8))
        except (TypeError, ValueError):
            top_k = 8
        top_k = max(1, min(25, top_k))

        # Pull every skill_id (NOT subject to the prefilter), score
        # against the query, return top matches. Reuses the
        # prefilter's tokenizer so the matching semantics agree with
        # the auto-prefilter — but augments it with a substring pass
        # so multilingual / mixed-language queries don't tie all
        # scores at 0.
        from xmclaw.skills.prefilter import _tokenize, _STOPWORDS, _score_skill

        all_skill_specs = []
        for sid in self._registry.list_skill_ids():
            sp = self._spec_for(sid)
            if sp is not None:
                all_skill_specs.append(sp)

        query_tokens = _tokenize(query) - _STOPWORDS
        q_lower = query.lower().strip()

        # Combined scoring: token-overlap (primary) + literal-substring
        # (secondary, weight 1.5 per matched token). Substring catches
        # the cases where the tokenizer split a useful CJK fragment
        # ("天气" → ["天", "气"]) but the actual literal "天气"
        # never appears in any English skill description, so token
        # overlap goes to 0 and we'd otherwise return alphabetical
        # noise. The substring pass also rewards exact id matches
        # (a query "deploy-vercel" hits "skill_deploy-vercel" via
        # both signals).
        scored: list[tuple[float, Any]] = []
        for sp in all_skill_specs:
            base = _score_skill(query_tokens, sp) if query_tokens else 0.0
            sub = 0.0
            if q_lower:
                hay = (sp.name + " " + (sp.description or "")).lower()
                if q_lower in hay:
                    # Whole-query substring → strongest signal. Pre-
                    # empts the token sum so a literal id match
                    # always wins over a partial token overlap.
                    sub += 5.0
                # Per-token substring as a fallback for the
                # tokenizer-misses-literal case described above.
                for tok in query_tokens:
                    if len(tok) >= 2 and tok in hay:
                        sub += 1.5
            scored.append((base + sub, sp))
        scored.sort(key=lambda x: x[0], reverse=True)
        # Unlike the auto-prefilter, we DON'T drop score=0 — the
        # whole point of skill_browse is to show the LLM what's
        # there even when overlap is weak. We do hard-cap at top_k
        # though, and tag scores so the LLM can read confidence.
        ranked = scored[:top_k]

        # Cheap structured payload — no markdown, no truncation; the
        # LLM reads JSON natively.
        out_list: list[dict[str, Any]] = []
        for score, sp in ranked:
            out_list.append({
                "tool_name": sp.name,
                "score": round(float(score), 3),
                "description": sp.description or "",
            })

        latency = self._elapsed_ms(t0)
        if not out_list:
            return ToolResult(
                call_id=call.id,
                ok=True,
                content={
                    "matches": [],
                    "note": (
                        f"No skills matched the query {query!r}. "
                        f"Total registered: {len(all_skill_specs)}. "
                        "Either fall back to bash / web_search / "
                        "file_* or rephrase the query with more "
                        "specific keywords (skill names + "
                        "descriptions are mostly English)."
                    ),
                },
                error=None,
                latency_ms=latency,
            )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "matches": out_list,
                "note": (
                    f"Showing top {len(out_list)} of "
                    f"{len(all_skill_specs)} skills. To invoke one, "
                    "call its ``tool_name`` directly on the next turn — "
                    "it will be in your tool list."
                ),
            },
            error=None,
            latency_ms=latency,
        )

    # ── registry-backed skill spec/invoke ──────────────────────────

    def _spec_for(self, skill_id: str) -> ToolSpec | None:
        try:
            ref = self._registry.ref(skill_id)
        except UnknownSkillError:
            return None

        manifest = ref.manifest
        description = self._build_description(skill_id, manifest, ref.version)
        return ToolSpec(
            name=_to_tool_name(skill_id),
            description=description,
            parameters_schema={
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Arguments forwarded to Skill.run(SkillInput(args=...)). "
                    "Pass whatever fields the skill's run() expects."
                ),
            },
        )

    def _build_description(
        self, skill_id: str, manifest, version: int
    ) -> str:
        """Assemble the human-readable description for one tool spec.

        Ordering (B-176 → B-177):
          1. Body description (the meat — what it DOES)
          2. "Use when ..." trigger hints
          3. Minimal id / provenance trailer
          4. Evidence line (audit-only)
        """
        parts: list[str] = []

        body = manifest.description.strip() if manifest.description else ""
        title = manifest.title.strip() if manifest.title else ""

        if body:
            parts.append(body)
        elif title and title != skill_id:
            # Fall back to title when no body — at least say SOMETHING
            # functional rather than just an id.
            parts.append(title)

        if manifest.triggers:
            triggers = ", ".join(repr(t) for t in manifest.triggers[:6])
            parts.append(f"Use when: {triggers}")

        # Trailer: minimal id reference + provenance (compact, not a headline).
        parts.append(f"[skill:{skill_id} v{version}, by={manifest.created_by}]")

        if manifest.evidence:
            parts.append("evidence: " + "; ".join(manifest.evidence))

        return "\n".join(parts)

    def _tool_name_to_skill_id(self, tool_name: str) -> str | None:
        """O(1) lookup from the lazy-built cache.

        Cache is rebuilt whenever it doesn't exist, so registry changes
        (promote / rollback) are always reflected on the next invoke.
        """
        if self._tool_name_cache is None:
            self._tool_name_cache = {
                _to_tool_name(sid): sid
                for sid in self._registry.list_skill_ids()
            }
        return self._tool_name_cache.get(tool_name)

    @staticmethod
    def _error_result(call_id: str, error: str, t0: float) -> ToolResult:
        return ToolResult(
            call_id=call_id, ok=False, content=None,
            error=error, latency_ms=SkillToolProvider._elapsed_ms(t0),
        )

    @staticmethod
    def _elapsed_ms(t0: float) -> float:
        return (time.perf_counter() - t0) * 1000.0


def _coerce_error(result: Any) -> str | None:
    """Pull a human-readable error string from a SkillOutput.result
    when the skill returned ok=False. Skills typically put ``error`` in
    the dict; fall back to repr otherwise so the agent gets SOMETHING
    to read instead of an empty string."""
    if isinstance(result, dict):
        for key in ("error", "message", "reason"):
            v = result.get(key)
            if isinstance(v, str) and v:
                return v
    return repr(result) if result is not None else "skill returned ok=False"
