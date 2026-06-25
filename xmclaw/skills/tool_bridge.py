"""SkillToolProvider — expose registered Skills as first-class tools.

This is **the only path** by which a skill becomes callable by the
agent. ``SkillRegistry`` HEAD entries (each one having passed
evidence-gated ``promote()`` — anti-req #12) get bridged to tools the
LLM picks like any other (``bash`` / ``file_read`` / etc.). The
registry is the trust boundary: no skill body is injected into the
system prompt, and no skill runs without first surviving HEAD
gating + (optional) variant_selector exploration.

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
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.skills.base import SkillContext, SkillInput
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
META_INSTALL_TOOL_NAME = "skill_install"
META_UNINSTALL_TOOL_NAME = "skill_uninstall"
# Epic #27 P0 G-01 (2026-05-19) — introspection tools so the agent can
# self-diagnose "did my install work?" / "what's in this skill?" instead
# of running list_dir + bash in circles.
META_STATUS_TOOL_NAME = "skill_status"
META_VIEW_TOOL_NAME = "skill_view"
META_DECISION_TOOL_NAME = "skill_decision"
# Epic #27 G-04 (2026-05-19) — progressive disclosure entry point. One
# unified ``skill_run(skill_id, args)`` tool that routes the call through
# the registry, same code path as the per-skill ``skill_<id>`` tools.
# In ``unified`` disclosure mode this is the ONLY way to invoke a skill
# — per-skill tools are not exposed to the LLM at all; in ``inline``
# mode it's an alias that coexists with them. The upstream agent / Claude-Skills
# 3-step flow is: skill_browse → skill_view → skill_run.
META_RUN_TOOL_NAME = "skill_run"
# Epic #27 P2 G-07 (2026-05-19) — versioned-edit history affordances.
# ``skill_diff`` shows a unified diff between the live file and the
# most-recent .versions/ snapshot. ``skill_rollback`` restores a
# snapshot in place (capturing the current live content first so
# the rollback itself is undoable). Both are always-on meta-tools
# and whitelisted in the prefilter so a "I just broke my skill"
# self-recovery loop always reaches the agent.
META_DIFF_TOOL_NAME = "skill_diff"
META_ROLLBACK_TOOL_NAME = "skill_rollback"
# Epic #27 P2 G-08 (2026-05-19) — self-evolving skills. The agent
# writes a new SKILL.md under ~/.xmclaw/v2/skills_user/<name>/ +
# stamps a .proposed.json marker so UserSkillsLoader assigns trust
# UNTRUSTED. Anti-cargo-cult: unlike the upstream agent' "5+ tool calls = save
# as skill" pattern (which auto-stages transient failures as
# learning), XMclaw keeps the agent's proposals at UNTRUSTED until
# explicit human / grader-evidence promotion. The bar is "the agent
# can write code on disk + see it loaded", not "the agent gets to
# self-promote whatever it just authored."
META_PROPOSE_TOOL_NAME = "skill_propose"
# Wave-33: skill composition — sequential workflow of multiple skills.
META_COMPOSE_TOOL_NAME = "skill_compose"

# Disclosure modes:
#   ``inline``  — legacy. Every registered skill shows up as its own
#                 ``skill_<id>`` tool (subject to prefilter top-K). The
#                 LLM picks them directly; ``skill_run`` is an alias.
#   ``unified`` — only the 6 meta-tools (browse / view / status /
#                 install / uninstall / run) are exposed. Forces the
#                 explicit discovery flow; saves the 50-tokens-per-skill
#                 description budget at the cost of one extra hop per
#                 invocation (browse + run vs. direct skill_<id>).
#   ``auto``    — switch to ``unified`` once registered skill count
#                 exceeds ``unified_threshold`` (default 20). Keeps the
#                 fast direct path for small setups + saves context
#                 once the registry grows large enough that the
#                 prefilter's top-12 is dropping useful matches.
DISCLOSURE_MODE_INLINE = "inline"
DISCLOSURE_MODE_UNIFIED = "unified"
DISCLOSURE_MODE_AUTO = "auto"
_VALID_DISCLOSURE_MODES = frozenset({
    DISCLOSURE_MODE_INLINE,
    DISCLOSURE_MODE_UNIFIED,
    DISCLOSURE_MODE_AUTO,
})
_DEFAULT_UNIFIED_THRESHOLD = 20


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


# ── MCP 安装上下文（2026-06-07）────────────────────────────────────────
# 由 app_lifespan 在建好 MCPHub 后调用 set_mcp_install_context(hub, config_path)。
# install 工具检测到 MCP server 时，用它把条目写进 config.mcp_servers 并热加载，
# 实现"装 MCP server 免重启"（用户要的全自动）。
_MCP_INSTALL_CTX: dict[str, Any] = {"hub": None, "config_path": None}


def set_mcp_install_context(hub: Any, config_path: Any) -> None:
    _MCP_INSTALL_CTX["hub"] = hub
    _MCP_INSTALL_CTX["config_path"] = config_path


async def _register_and_hotload_mcp(server_id: str, mcp_config: dict) -> dict:
    """把 MCP server 落盘进 config.mcp_servers 并热加载到运行中的 hub。

    返回 ``{"persisted","hot_loaded","status","restart_required","error?"}``。
    任何一步失败都不抛——把结果如实回给 agent/用户。
    """
    import json as _json
    out: dict[str, Any] = {"persisted": False, "hot_loaded": False,
                           "status": None, "restart_required": True}
    hub = _MCP_INSTALL_CTX.get("hub")
    cfg_path = _MCP_INSTALL_CTX.get("config_path")

    # 1) 落盘
    servers: dict[str, Any] = {}
    if cfg_path:
        try:
            from pathlib import Path as _Path
            p = _Path(cfg_path)
            cfg = _json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
            if not isinstance(cfg, dict):
                cfg = {}
            servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
            servers[server_id] = mcp_config
            cfg["mcp_servers"] = servers
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(_json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            out["persisted"] = True
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"persist failed: {exc}"
    else:
        servers = {server_id: mcp_config}

    # 2) 热加载（hub 在则免重启）
    if hub is not None and hasattr(hub, "reload_from_config"):
        try:
            statuses = await hub.reload_from_config(servers)
            out["status"] = statuses.get(server_id) if isinstance(statuses, dict) else None
            out["hot_loaded"] = out["status"] == "connected"
            out["restart_required"] = not out["hot_loaded"]
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"hot-load failed: {exc}"
    return out


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
        watcher: Any = None,
        disclosure_mode: str = DISCLOSURE_MODE_AUTO,
        unified_threshold: int = _DEFAULT_UNIFIED_THRESHOLD,
        block_critical_installs: bool = True,
    ) -> None:
        self._registry = registry
        self._description_prefix = description_prefix
        self._tool_name_cache: dict[str, str] | None = None
        self._block_critical_installs = block_critical_installs
        # B-295: opt-in variant selector for UCB1 over (skill_id, version)
        # arms. None → always HEAD (legacy behaviour). When wired, each
        # invocation asks the selector which version to run; stats
        # accumulate via the selector's own GRADER_VERDICT subscription.
        self._variant_selector = variant_selector
        # Epic #27 P0 G-01 (2026-05-19): SkillsWatcher reference so the
        # introspection meta-tools (``skill_status``) can surface load
        # failures + pending restarts to the agent. None when wired in
        # contexts without a daemon watcher (tests, eval harness).
        self._watcher = watcher
        # Epic #27 G-04 (2026-05-19): progressive-disclosure switch.
        # Unknown values fall back to ``auto`` rather than raising — this
        # path runs at daemon boot and we'd rather log + continue than
        # refuse to start over a config typo.
        mode = (disclosure_mode or DISCLOSURE_MODE_AUTO).strip().lower()
        if mode not in _VALID_DISCLOSURE_MODES:
            mode = DISCLOSURE_MODE_AUTO
        self._disclosure_mode = mode
        try:
            self._unified_threshold = max(
                0, int(unified_threshold),
            )
        except (TypeError, ValueError):
            self._unified_threshold = _DEFAULT_UNIFIED_THRESHOLD

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_tools(self) -> list[ToolSpec]:
        # Invalidate the tool-name cache so that any registry mutation
        # (new registration, promote, rollback, uninstall) is visible
        # on the next invoke() — otherwise list_tools() shows the new
        # skill but invoke() returns "unknown skill tool" because the
        # stale cache doesn't contain it.
        self._invalidate_tool_name_cache()

        specs = []
        # B-299: prepend the meta-discovery tool so the LLM can ask
        # "is there a skill for X?" out-of-band when the prefilter's
        # token-overlap match misses (CJK queries hitting English
        # skill descs is the canonical 0-result case). The prefilter
        # special-cases this name to always pass through.
        specs.append(self._browse_spec())
        # Wave-27 fix-LAT7: skill_install + skill_uninstall let the
        # agent expand its own toolset on demand, instead of needing
        # the human to run ``xmclaw skill install`` from the CLI.
        # Both are always exposed (whitelisted in prefilter same way
        # as skill_browse) so the agent can act on its own "this
        # repo looks useful" decisions.
        specs.append(self._install_spec())
        specs.append(self._uninstall_spec())
        # Epic #27 P0 G-01 (2026-05-19): introspection. The agent
        # needs to be able to ask "what's the registry state right
        # now?" + "what's inside <skill_id>?" without running
        # list_dir / bash. Both meta-tools.
        specs.append(self._status_spec())
        specs.append(self._view_spec())
        specs.append(self._decision_spec())
        # Epic #27 G-04 (2026-05-19): unified skill_run dispatcher.
        # Always exposed regardless of disclosure mode — in ``inline``
        # it's an alias the LLM may use; in ``unified`` it's the only
        # invocation path. Keeping it always-on means we don't have to
        # update the prompt / TOOLS.md auto-block when the mode flips.
        specs.append(self._run_spec())
        # Epic #27 G-07 (2026-05-19): versioned-edit history. Always
        # exposed so the recovery loop ("I just broke my skill, can
        # you put it back?") always has the tools to act.
        specs.append(self._diff_spec())
        specs.append(self._rollback_spec())
        # Epic #27 G-08 (2026-05-19): self-evolving skills. Agent
        # can author + register a new skill on disk under
        # ``~/.xmclaw/v2/skills_user/<name>/``. Marker file keeps
        # trust at UNTRUSTED until manual promotion.
        specs.append(self._propose_spec())
        # Wave-33: skill composition — sequential workflow execution.
        specs.append(self._compose_spec())

        if self._effective_disclosure_mode() == DISCLOSURE_MODE_UNIFIED:
            # Skip per-skill tools entirely. The LLM discovers + invokes
            # via skill_browse → skill_view → skill_run. Big context win
            # on large libraries (~50 tokens × N skills).
            return specs

        for skill_id in self._registry.list_skill_ids():
            spec = self._spec_for(skill_id)
            if spec is not None:
                specs.append(spec)
        return specs

    def _effective_disclosure_mode(self) -> str:
        """Resolve the ``auto`` mode against the current registry size.

        Counts skill IDs (not versions) — version multiplicity doesn't
        change the per-LLM-turn surface since only HEAD is exposed.
        Errors fall back to ``inline`` (the safer default — agent still
        sees per-skill tools, no regression).
        """
        if self._disclosure_mode == DISCLOSURE_MODE_INLINE:
            return DISCLOSURE_MODE_INLINE
        if self._disclosure_mode == DISCLOSURE_MODE_UNIFIED:
            return DISCLOSURE_MODE_UNIFIED
        try:
            count = sum(1 for _ in self._registry.list_skill_ids())
        except Exception:  # noqa: BLE001
            return DISCLOSURE_MODE_INLINE
        return (
            DISCLOSURE_MODE_UNIFIED
            if count > self._unified_threshold
            else DISCLOSURE_MODE_INLINE
        )

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
        if call.name == META_INSTALL_TOOL_NAME:
            return await self._invoke_install(call, t0)
        if call.name == META_UNINSTALL_TOOL_NAME:
            return self._invoke_uninstall(call, t0)
        if call.name == META_STATUS_TOOL_NAME:
            return self._invoke_status(call, t0)
        if call.name == META_VIEW_TOOL_NAME:
            return self._invoke_view(call, t0)
        if call.name == META_DECISION_TOOL_NAME:
            return self._invoke_decision(call, t0)
        if call.name == META_DIFF_TOOL_NAME:
            return self._invoke_diff(call, t0)
        if call.name == META_ROLLBACK_TOOL_NAME:
            return self._invoke_rollback(call, t0)
        if call.name == META_PROPOSE_TOOL_NAME:
            return self._invoke_propose(call, t0)
        if call.name == META_COMPOSE_TOOL_NAME:
            return await self._invoke_compose(call, t0)

        # Epic #27 G-04: skill_run reads ``skill_id`` from args and
        # falls through to the shared invocation path below. This
        # keeps variant selection + version metadata stamping +
        # error coercion identical to the per-skill route.
        if call.name == META_RUN_TOOL_NAME:
            args = dict(call.args or {})
            requested = args.get("skill_id")
            if not isinstance(requested, str) or not requested.strip():
                return self._error_result(
                    call.id,
                    "skill_run requires 'skill_id' string in args",
                    t0,
                )
            skill_id = requested.strip()
            # Forward only ``args`` to the skill; ``skill_id`` is
            # consumed by the dispatcher itself, not by the skill.
            forwarded_args = args.get("args")
            if isinstance(forwarded_args, dict):
                call_args = forwarded_args
            elif forwarded_args is None:
                call_args = {}
            else:
                return self._error_result(
                    call.id,
                    "skill_run 'args' must be an object (or omitted)",
                    t0,
                )
        else:
            skill_id = self._tool_name_to_skill_id(call.name)
            if skill_id is None:
                return self._error_result(
                    call.id, f"unknown skill tool: {call.name!r}", t0
                )
            call_args = dict(call.args or {})

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

        # Wave-33: inject SkillContext so skills can introspect the
        # registry (read-only).  Backward-compatible: only pass ``ctx``
        # when the concrete subclass accepts it.
        import inspect as _inspect
        _ctx = SkillContext(_registry=self._registry)
        _sig = _inspect.signature(skill.run)
        _has_ctx = "ctx" in _sig.parameters
        try:
            if _has_ctx:
                out = await skill.run(SkillInput(args=call_args), ctx=_ctx)
            else:
                out = await skill.run(SkillInput(args=call_args))
        except Exception as exc:  # noqa: BLE001
            latency_ms = self._elapsed_ms(t0)
            self._registry.record_usage(skill_id, success=False, latency_ms=latency_ms)
            return self._error_result(
                call.id, f"{type(exc).__name__}: {exc}", t0
            )

        # Record usage statistics for this invocation.
        latency_ms = self._elapsed_ms(t0)
        self._registry.record_usage(skill_id, success=bool(out.ok), latency_ms=latency_ms)

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
            latency_ms=latency_ms,
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

    # ── Epic #27 P0 G-01: introspection meta-tools ─────────────────

    def _status_spec(self) -> ToolSpec:
        """ToolSpec for ``skill_status``. Pure read, no I/O, never
        raises. The first call the agent should make when it suspects
        "did my install succeed?" / "why isn't this skill loading?"."""
        return ToolSpec(
            name=META_STATUS_TOOL_NAME,
            description=(
                "Inspect the live skill registry state — call this BEFORE "
                "running list_dir / bash to debug a missing skill. "
                "Returns ``{registered, load_failures, pending_restarts, "
                "totals}``:\n"
                "  - ``registered``: count of (skill_id, version) pairs "
                "currently in the SkillRegistry.\n"
                "  - ``load_failures``: list of skills the daemon TRIED to "
                "load but couldn't (broken skill.py, missing manifest, "
                "etc). Each row has ``skill_id / path / kind / error / "
                "ticks_failing``. If your skill ISN'T in skill_browse but "
                "IS listed here, the user's daemon hit an error during "
                "load — read .error and fix the underlying issue.\n"
                "  - ``pending_restarts``: list of Python skills whose "
                "``skill.py`` was edited but daemon still has the cached "
                "import. Each row has ``skill_id / version / path / "
                "state``. When ``state='fixed_after_failure'`` it means "
                "the user fixed a broken skill but daemon hasn't reloaded "
                "yet — tell them ``xmclaw stop && xmclaw start`` (or "
                "click the restart button in the UI) is needed.\n"
                "When neither array has rows AND your skill is missing, "
                "the most likely cause is the directory not being under "
                "``~/.xmclaw/skills_user/`` or ``~/.agents/skills/`` — "
                "verify with ``skill_view`` first."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )

    def _invoke_status(self, call: ToolCall, t0: float) -> ToolResult:
        """Return load_failures + pending_restarts + counts. Synchronous;
        no I/O; never raises."""
        registered_count = 0
        try:
            registered_count = sum(
                1 for sid in self._registry.list_skill_ids()
                for _v in self._registry.list_versions(sid)
            )
        except Exception:  # noqa: BLE001
            registered_count = 0

        load_failures: list[dict[str, Any]] = []
        pending_restarts: list[dict[str, Any]] = []
        if self._watcher is not None:
            try:
                load_failures = list(self._watcher.load_failures() or [])
            except Exception:  # noqa: BLE001
                load_failures = []
            try:
                pending_restarts = list(self._watcher.pending_restarts() or [])
            except Exception:  # noqa: BLE001
                pending_restarts = []

        # Sort failures by ticks_failing desc (oldest pain first).
        load_failures.sort(
            key=lambda r: int(r.get("ticks_failing", 0) or 0),
            reverse=True,
        )

        registered: list[dict[str, Any]] = []
        try:
            for sid in self._registry.list_skill_ids():
                ref = self._registry.ref(sid)
                manifest = ref.manifest
                registered.append({
                    "skill_id": sid,
                    "active_version": ref.version,
                    "versions": self._registry.list_versions(sid),
                    "title": manifest.title or sid,
                    "description": manifest.description,
                    "when_to_use": getattr(manifest, "when_to_use", ""),
                    "triggers": list(getattr(manifest, "triggers", ()) or ()),
                    "trust_level": str(getattr(manifest, "trust_level", "")),
                    "requires_restart": bool(
                        getattr(manifest, "requires_restart", False),
                    ),
                })
        except Exception:  # noqa: BLE001
            registered = []

        roots: list[dict[str, Any]] = []
        try:
            from xmclaw.skills.user_loader import resolve_skill_roots

            canonical, extras = resolve_skill_roots()
            for kind, root in [("canonical", canonical), *[
                ("extra", p) for p in extras
            ]]:
                path = Path(root).expanduser()
                entries: list[dict[str, Any]] = []
                if path.exists() and path.is_dir():
                    for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                        if not child.is_dir() or child.name.startswith("."):
                            continue
                        entries.append({
                            "id": child.name,
                            "has_skill_md": (child / "SKILL.md").exists(),
                            "has_manifest_json": (child / "manifest.json").exists(),
                            "has_skill_py": (child / "skill.py").exists(),
                            "path": str(child),
                        })
                roots.append({
                    "kind": kind,
                    "path": str(path),
                    "exists": path.exists(),
                    "skill_dirs": entries,
                    "skill_dir_count": len(entries),
                })
        except Exception as exc:  # noqa: BLE001
            roots = [{
                "kind": "error",
                "path": "",
                "exists": False,
                "skill_dirs": [],
                "skill_dir_count": 0,
                "error": str(exc),
            }]

        notes: list[str] = []
        if load_failures:
            n = len(load_failures)
            notes.append(
                f"⚠ {n} skill(s) failed to load — read their .error "
                "field for the underlying cause, then file_read the "
                ".path to see the broken source."
            )
        if pending_restarts:
            n = len(pending_restarts)
            fixed = sum(
                1 for r in pending_restarts
                if r.get("state") == "fixed_after_failure"
            )
            if fixed:
                notes.append(
                    f"⚠ {fixed} skill(s) appear FIXED but daemon still "
                    "has the broken cached import. Tell the user to "
                    "restart the daemon (xmclaw stop && xmclaw start)."
                )
            elif n:
                notes.append(
                    f"ℹ {n} Python skill(s) edited — daemon restart "
                    "needed before changes take effect."
                )
        if not notes:
            notes.append(
                "✓ No load failures, no pending restarts. Registry is "
                "in a clean state."
            )

        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "totals": {
                    "registered_versions": registered_count,
                    "registered_skill_ids": len(
                        list(self._registry.list_skill_ids()),
                    ),
                    "load_failures": len(load_failures),
                    "pending_restarts": len(pending_restarts),
                },
                "registered": registered,
                "roots": roots,
                "load_failures": load_failures,
                "pending_restarts": pending_restarts,
                "notes": notes,
            },
            error=None,
            latency_ms=self._elapsed_ms(t0),
        )

    def _decision_spec(self) -> ToolSpec:
        """ToolSpec for structured skill routing decisions."""
        return ToolSpec(
            name=META_DECISION_TOOL_NAME,
            description=(
                "Record your structured skill routing decision for this turn. "
                "Call this when the skill-discovery block asks you to use a "
                "candidate skill, browse the catalog, or skip candidates. "
                "Use action='use' before invoking a matching skill, "
                "action='skip' before falling back to generic tools, or "
                "action='browse' before calling skill_browse. This tool has no "
                "side effects; it exists so the UI/event log can show selected "
                "skill and concrete skip_reason instead of relying on prose."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["use", "skip", "browse"],
                    },
                    "skill_id": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "skip_reason": {
                        "type": "string",
                        "description": (
                            "Required for action='skip'. Use one of the "
                            "allowed skip_reasons from the skill-discovery block."
                        ),
                    },
                    "browse_query": {
                        "type": "string",
                        "description": (
                            "Required for action='browse'. Query you will pass "
                            "to skill_browse."
                        ),
                    },
                    "note": {"type": "string"},
                },
            },
        )

    def _invoke_decision(self, call: ToolCall, t0: float) -> ToolResult:
        args = dict(call.args or {})
        action = str(args.get("action") or "").strip().lower()
        if action not in {"use", "skip", "browse"}:
            return self._error_result(
                call.id,
                "skill_decision requires action in {'use','skip','browse'}",
                t0,
            )
        skill_id = str(args.get("skill_id") or "").strip()
        tool_name = str(args.get("tool_name") or "").strip()
        skip_reason = str(args.get("skip_reason") or "").strip()
        browse_query = str(args.get("browse_query") or "").strip()
        note = str(args.get("note") or "").strip()
        if action == "skip" and not skip_reason:
            return self._error_result(
                call.id,
                "skill_decision(action='skip') requires skip_reason",
                t0,
            )
        if action == "browse" and not browse_query:
            return self._error_result(
                call.id,
                "skill_decision(action='browse') requires browse_query",
                t0,
            )
        if action == "use" and not (skill_id or tool_name):
            return self._error_result(
                call.id,
                "skill_decision(action='use') requires skill_id or tool_name",
                t0,
            )
        content = {
            "kind": "skill_decision",
            "action": action,
            "skill_id": skill_id,
            "tool_name": tool_name,
            "skip_reason": skip_reason,
            "browse_query": browse_query,
            "note": note,
            "ok_to_continue": True,
        }
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=content,
            error=None,
            latency_ms=self._elapsed_ms(t0),
            metadata={"kind": "skill_decision", "action": action},
        )

    def _view_spec(self) -> ToolSpec:
        """ToolSpec for ``skill_view``. Reads the skill directory + its
        files so the agent can inspect SKILL.md content, skill.py
        source, or manifest.json without poking around the disk."""
        return ToolSpec(
            name=META_VIEW_TOOL_NAME,
            description=(
                "Read the files inside an installed skill directory. "
                "Use when you want to inspect HOW a skill works before "
                "invoking it, OR when ``skill_status`` reports a load "
                "failure and you need to see the broken source.\n"
                "Modes:\n"
                "  - ``skill_view(skill_id)``: lists the skill dir's "
                "files + returns SKILL.md (or skill.py) body up to 8KB.\n"
                "  - ``skill_view(skill_id, file_path)``: returns the "
                "specific file's content (path is relative to the skill "
                "dir; rejects ``..`` and absolute paths).\n"
                "Returns ``{path, kind, files, body?}``. ``body`` is the "
                "primary file's content (or the requested file_path)."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Directory name of the skill.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Optional: relative path inside the skill "
                            "dir (``references/api.md``). Omit to read "
                            "the primary file (SKILL.md or skill.py)."
                        ),
                    },
                },
            },
        )

    def _invoke_view(self, call: ToolCall, t0: float) -> ToolResult:
        """Find the skill dir + read the requested file. Defaults to
        SKILL.md or skill.py at the top of the dir. Caps body at 8KB."""
        from pathlib import Path as _Path

        args = dict(call.args or {})
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            return self._error_result(
                call.id,
                "skill_view requires a 'skill_id' string",
                t0,
            )
        skill_id = skill_id.strip()
        file_path = args.get("file_path")
        if file_path is not None and not isinstance(file_path, str):
            file_path = None
        if isinstance(file_path, str):
            file_path = file_path.strip()
            # Reject path traversal + absolute paths. ``pathlib.Path``
            # treats POSIX-absolute paths like ``/etc/passwd`` as
            # RELATIVE on Windows (only ``C:\`` etc count as absolute
            # there), so we string-check both flavors directly. Also
            # block UNC ``\\server\share``.
            looks_absolute = (
                _Path(file_path).is_absolute()
                or file_path.startswith("/")
                or file_path.startswith("\\\\")
                or (
                    len(file_path) >= 2
                    and file_path[1] == ":"
                    and file_path[0].isalpha()
                )
            )
            if ".." in _Path(file_path).parts or looks_absolute:
                return self._error_result(
                    call.id,
                    f"file_path must be relative + no ..: got {file_path!r}",
                    t0,
                )

        # Resolve the skill dir against the canonical roots.
        from xmclaw.skills.user_loader import resolve_skill_roots

        try:
            canonical, extras = resolve_skill_roots()
        except Exception:  # noqa: BLE001
            from xmclaw.utils.paths import user_skills_dir
            canonical, extras = (user_skills_dir(), [])
        candidate_roots = [canonical, *extras]
        skill_dir: _Path | None = None
        for root in candidate_roots:
            cand = root / skill_id
            if cand.is_dir():
                skill_dir = cand
                break
        if skill_dir is None:
            return self._error_result(
                call.id,
                f"skill dir not found for {skill_id!r} under any of "
                f"{[str(r) for r in candidate_roots]}",
                t0,
            )

        # List top-level files (and one level into versions/ + skills/
        # if present) for the file inventory.
        files: list[dict[str, Any]] = []
        try:
            for entry in sorted(skill_dir.iterdir()):
                if entry.name.startswith("__pycache__"):
                    continue
                files.append({
                    "name": entry.name,
                    "kind": "dir" if entry.is_dir() else "file",
                    "size": (
                        entry.stat().st_size if entry.is_file() else None
                    ),
                })
        except OSError as exc:
            return self._error_result(
                call.id,
                f"cannot list {skill_dir}: {exc}",
                t0,
            )

        # Pick the file to read.
        target: _Path
        if isinstance(file_path, str) and file_path:
            target = skill_dir / file_path
            if not target.exists():
                return self._error_result(
                    call.id,
                    f"file_path {file_path!r} not found in {skill_id}",
                    t0,
                )
        else:
            # Default: SKILL.md preferred, fall back to skill.py.
            if (skill_dir / "SKILL.md").is_file():
                target = skill_dir / "SKILL.md"
            elif (skill_dir / "skill.py").is_file():
                target = skill_dir / "skill.py"
            elif (skill_dir / "manifest.json").is_file():
                target = skill_dir / "manifest.json"
            else:
                return ToolResult(
                    call_id=call.id,
                    ok=True,
                    content={
                        "path": str(skill_dir),
                        "kind": "dir",
                        "files": files,
                        "body": None,
                        "note": (
                            "No SKILL.md / skill.py / manifest.json at "
                            "the top. Pass file_path explicitly to read "
                            "something specific."
                        ),
                    },
                    error=None,
                    latency_ms=self._elapsed_ms(t0),
                )

        # Read with 8KB cap so a 5MB SKILL.md doesn't blow context.
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self._error_result(
                call.id,
                f"cannot read {target}: {exc}",
                t0,
            )
        truncated = len(raw) > 8192
        body = raw[:8192] + (
            "\n\n[... truncated; file is "
            f"{len(raw)} bytes total. Use file_path with a more "
            "specific reference to read the rest.]"
            if truncated else ""
        )

        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "path": str(target),
                "skill_dir": str(skill_dir),
                "kind": (
                    "markdown" if target.name == "SKILL.md"
                    else "python" if target.name == "skill.py"
                    else "manifest" if target.name == "manifest.json"
                    else "other"
                ),
                "files": files,
                "body": body,
                "truncated": truncated,
                "size_bytes": len(raw),
            },
            error=None,
            latency_ms=self._elapsed_ms(t0),
        )

    # ── Epic #27 G-07: versioned-edit history (diff + rollback) ────

    def _diff_spec(self) -> ToolSpec:
        return ToolSpec(
            name=META_DIFF_TOOL_NAME,
            description=(
                "Read a unified diff between an installed skill's "
                "current SKILL.md / skill.py and a prior snapshot from "
                "its ``.versions/`` history. Use when ``skill_status`` "
                "reports a load failure right after a recent edit, or "
                "when you want to know what just changed before "
                "deciding whether to rollback. Returns ``{path, "
                "snapshot, diff, snapshots_total}``; ``diff`` is empty "
                "if current content equals the snapshot. The watcher "
                "automatically snapshots on every detected save, so "
                "the freshest history exists at index 0 (the previous "
                "save) — index 1 = save before that, etc."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Installed skill id.",
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional: ``SKILL.md`` (default), "
                            "``skill.py``, or ``manifest.json``. The "
                            "file under the skill dir whose snapshot "
                            "history to consult."
                        ),
                    },
                    "against_index": {
                        "type": "integer",
                        "description": (
                            "Newest-first index into "
                            "``.versions/`` (default 0 = the most "
                            "recent prior save)."
                        ),
                        "minimum": 0,
                    },
                },
            },
        )

    def _rollback_spec(self) -> ToolSpec:
        return ToolSpec(
            name=META_ROLLBACK_TOOL_NAME,
            description=(
                "Restore an installed skill's SKILL.md / skill.py / "
                "manifest.json from its ``.versions/`` snapshot "
                "history, overwriting the live file in place. The "
                "rollback ITSELF captures the current live content "
                "first (snapshotted under .versions/ same as a normal "
                "edit) so it's undoable — call ``skill_rollback`` "
                "again with index 0 to swap back. For SKILL.md / "
                "manifest.json the SkillsWatcher will re-process the "
                "change on the next tick; for skill.py a daemon "
                "restart MAY be needed if the import was cached "
                "(``skill_status`` will say so). Returns ``{path, "
                "restored_from, snapshots_total}``."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Installed skill id.",
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Same as ``skill_diff.file``: which file "
                            "(SKILL.md default, or skill.py / "
                            "manifest.json)."
                        ),
                    },
                    "to_index": {
                        "type": "integer",
                        "description": (
                            "Snapshot index to restore (default 0)."
                        ),
                        "minimum": 0,
                    },
                },
            },
        )

    def _resolve_skill_file(
        self, skill_id: str, file_hint: str | None,
    ) -> "tuple[Any | None, Any | None, str | None]":
        """Locate the skill dir + the specific file under it that
        ``skill_diff`` / ``skill_rollback`` should consult.

        Returns ``(skill_dir, target_file, error)``. On success
        ``error`` is ``None``. ``target_file`` defaults to ``SKILL.md``
        when present else ``skill.py`` — same precedence as
        ``skill_view`` so the LLM doesn't have to learn a second
        convention.
        """
        from pathlib import Path as _Path
        from xmclaw.skills.user_loader import resolve_skill_roots

        try:
            canonical, extras = resolve_skill_roots()
        except Exception:  # noqa: BLE001
            from xmclaw.utils.paths import user_skills_dir
            canonical, extras = (user_skills_dir(), [])
        roots = [canonical, *extras]
        skill_dir = None
        for root in roots:
            cand = root / skill_id
            if cand.is_dir():
                skill_dir = cand
                break
        if skill_dir is None:
            return None, None, (
                f"skill dir not found for {skill_id!r} under any of "
                f"{[str(r) for r in roots]}"
            )

        if file_hint:
            # Reject path-traversal — same checks as skill_view.
            hint = file_hint.strip()
            looks_absolute = (
                _Path(hint).is_absolute()
                or hint.startswith("/")
                or hint.startswith("\\\\")
                or (
                    len(hint) >= 2 and hint[1] == ":"
                    and hint[0].isalpha()
                )
            )
            if ".." in _Path(hint).parts or looks_absolute:
                return None, None, (
                    f"file must be relative + no ..: got {hint!r}"
                )
            target = skill_dir / hint
        else:
            if (skill_dir / "SKILL.md").is_file():
                target = skill_dir / "SKILL.md"
            elif (skill_dir / "skill.py").is_file():
                target = skill_dir / "skill.py"
            elif (skill_dir / "manifest.json").is_file():
                target = skill_dir / "manifest.json"
            else:
                return None, None, (
                    f"no SKILL.md / skill.py / manifest.json found "
                    f"in {skill_dir}; pass ``file`` explicitly"
                )
        if not target.is_file():
            return None, None, f"file not found: {target}"
        return skill_dir, target, None

    def _invoke_diff(self, call: ToolCall, t0: float) -> ToolResult:
        from xmclaw.skills.version_history import diff as _diff
        from xmclaw.skills.version_history import list_versions

        args = dict(call.args or {})
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            return self._error_result(
                call.id, "skill_diff requires 'skill_id'", t0,
            )
        file_hint = args.get("file")
        if file_hint is not None and not isinstance(file_hint, str):
            file_hint = None
        try:
            against_index = int(args.get("against_index", 0))
        except (TypeError, ValueError):
            against_index = 0
        against_index = max(0, against_index)

        skill_dir, target, err = self._resolve_skill_file(
            skill_id.strip(), file_hint,
        )
        if err:
            return self._error_result(call.id, err, t0)

        ext = target.suffix.lstrip(".") or "txt"
        snapshots = list_versions(skill_dir, ext=ext)
        if not snapshots:
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "path": str(target),
                    "snapshot": None,
                    "diff": "",
                    "snapshots_total": 0,
                    "note": (
                        "No snapshots exist yet for this skill. The "
                        "watcher writes one on every detected save; "
                        "if this is the first time you've touched "
                        "the file since install, there's nothing to "
                        "diff against."
                    ),
                },
                error=None, latency_ms=self._elapsed_ms(t0),
            )
        if against_index >= len(snapshots):
            return self._error_result(
                call.id,
                f"against_index={against_index} out of range; "
                f"only {len(snapshots)} snapshot(s) exist",
                t0,
            )
        diff_body = _diff(
            skill_dir, target,
            against_index=against_index, max_lines=200,
        )
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "path": str(target),
                "snapshot": str(snapshots[against_index].path),
                "diff": diff_body or "",
                "snapshots_total": len(snapshots),
            },
            error=None, latency_ms=self._elapsed_ms(t0),
        )

    def _invoke_rollback(self, call: ToolCall, t0: float) -> ToolResult:
        from xmclaw.skills.version_history import (
            list_versions, rollback as _rollback,
        )

        args = dict(call.args or {})
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            return self._error_result(
                call.id, "skill_rollback requires 'skill_id'", t0,
            )
        file_hint = args.get("file")
        if file_hint is not None and not isinstance(file_hint, str):
            file_hint = None
        try:
            to_index = int(args.get("to_index", 0))
        except (TypeError, ValueError):
            to_index = 0
        to_index = max(0, to_index)

        skill_dir, target, err = self._resolve_skill_file(
            skill_id.strip(), file_hint,
        )
        if err:
            return self._error_result(call.id, err, t0)

        restored_path = _rollback(
            skill_dir, target,
            to_index=to_index, snapshot_current=True,
        )
        if restored_path is None:
            ext = target.suffix.lstrip(".") or "txt"
            total = len(list_versions(skill_dir, ext=ext))
            return self._error_result(
                call.id,
                f"rollback failed — no snapshot at index "
                f"{to_index} (have {total}) or IO error",
                t0,
            )
        ext = target.suffix.lstrip(".") or "txt"
        total = len(list_versions(skill_dir, ext=ext))
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "path": str(target),
                "restored_from": str(restored_path),
                "snapshots_total": total,
                "note": (
                    "Live file restored from snapshot. The "
                    "SkillsWatcher will pick up the new mtime on "
                    "its next tick (typically < 5s). For Python "
                    "skill.py, daemon restart may be required if "
                    "the import was cached (``skill_status`` will "
                    "indicate via pending_restarts)."
                ),
            },
            error=None, latency_ms=self._elapsed_ms(t0),
            side_effects=(str(target),),
        )

    # ── Epic #27 G-08: self-evolving skills (skill_propose) ────────

    def _propose_spec(self) -> ToolSpec:
        return ToolSpec(
            name=META_PROPOSE_TOOL_NAME,
            description=(
                "Propose a NEW skill on disk. Writes "
                "``~/.xmclaw/v2/skills_user/<name>/SKILL.md`` + a "
                "``.proposed.json`` marker so the loader assigns "
                "trust UNTRUSTED until manual review.\n"
                "Use when you've solved a problem with a non-trivial "
                "sequence of tools and want a future-you to be able "
                "to re-use the playbook — write the SKILL.md body "
                "describing the recipe.\n"
                "Anti-pattern (don't do this): proposing a skill for "
                "a one-off task ('this user wanted X just now'). The "
                "proposal mechanism is for genuinely-recurring "
                "playbooks the agent will benefit from later.\n"
                "After ``skill_propose`` returns, the daemon's "
                "SkillsWatcher picks up the new file on its next "
                "tick (typically < 5s) and registers it; "
                "``skill_status`` will then show ``trust=untrusted`` "
                "for the new entry. Returns ``{skill_id, path, "
                "trust, note}``."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "body"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Slug for the new skill, becomes the "
                            "directory name + tool name. Must match "
                            "``[a-z0-9][a-z0-9_-]{0,40}`` — short, "
                            "kebab-case, no spaces. Refused when a "
                            "skill of that id already exists."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "SKILL.md content. Conventional shape: "
                            "frontmatter ``---\\nname: <name>\\n"
                            "description: <one line>\\n---`` followed "
                            "by markdown describing what the skill "
                            "does + how to invoke it. Must be at "
                            "least 50 chars (defensive against "
                            "empty-stub proposals)."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional one-line summary; if provided "
                            "and the body has no frontmatter, will "
                            "be prepended as a minimal frontmatter "
                            "block."
                        ),
                    },
                },
            },
        )

    def _invoke_propose(self, call: ToolCall, t0: float) -> ToolResult:
        import json as _json
        import re as _re
        import time as _time
        from pathlib import Path as _Path

        args = dict(call.args or {})
        name = args.get("name")
        body = args.get("body")
        description = args.get("description") or ""
        if not isinstance(name, str) or not name.strip():
            return self._error_result(
                call.id, "skill_propose requires 'name'", t0,
            )
        if not isinstance(body, str) or len(body.strip()) < 50:
            return self._error_result(
                call.id,
                "skill_propose 'body' must be at least 50 non-blank "
                "chars (defensive against empty-stub proposals)",
                t0,
            )
        name = name.strip().lower()
        if not _re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
            return self._error_result(
                call.id,
                f"skill_propose 'name' {name!r} doesn't match "
                "[a-z0-9][a-z0-9_-]{0,40} (short kebab-case slug)",
                t0,
            )

        # Resolve where to write — use the canonical user-skills root.
        try:
            from xmclaw.utils.paths import user_skills_dir
            root = user_skills_dir()
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                call.id,
                f"could not resolve user_skills_dir: "
                f"{type(exc).__name__}: {exc}", t0,
            )

        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._error_result(
                call.id,
                f"cannot create user_skills_dir {root}: {exc}", t0,
            )

        skill_dir = root / name
        if skill_dir.exists():
            return self._error_result(
                call.id,
                f"skill {name!r} already exists at {skill_dir}; "
                "rename your proposal or use skill_diff + "
                "skill_rollback to edit the existing one",
                t0,
            )

        # If the body has no frontmatter and description is provided,
        # prepend a minimal frontmatter so the SkillsLoader can read
        # the description without parsing markdown.
        final_body = body
        if not body.lstrip().startswith("---") and description.strip():
            final_body = (
                f"---\n"
                f"name: {name}\n"
                f"description: {description.strip()}\n"
                f"---\n\n"
                + body
            )

        try:
            skill_dir.mkdir(parents=True, exist_ok=False)
            (skill_dir / "SKILL.md").write_text(
                final_body, encoding="utf-8",
            )
            marker = {
                "proposed_by": "agent",
                "proposed_at": _time.time(),
                "evidence_count": 0,
                "promote_after_evidence": 3,
                "note": (
                    "Agent-authored skill. Trust=untrusted until "
                    "manual review removes this marker file."
                ),
            }
            (skill_dir / ".proposed.json").write_text(
                _json.dumps(marker, indent=2), encoding="utf-8",
            )
        except OSError as exc:
            return self._error_result(
                call.id,
                f"cannot write skill files: {type(exc).__name__}: {exc}",
                t0,
            )

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "skill_id": name,
                "path": str(skill_dir),
                "trust": "untrusted",
                "note": (
                    f"Skill {name!r} written to {skill_dir}. The "
                    "SkillsWatcher will pick it up within ~5s and "
                    "register it as trust=untrusted. To promote it "
                    "later: remove ``.proposed.json`` and restart "
                    "the daemon (or wait for an evidence-based "
                    "promotion via the HonestGrader once the skill "
                    "accumulates enough successful invocations)."
                ),
            },
            error=None, latency_ms=self._elapsed_ms(t0),
            side_effects=(str(skill_dir),),
        )

    # ── Wave-33: skill composition (sequential workflow) ─────────

    def _compose_spec(self) -> ToolSpec:
        return ToolSpec(
            name=META_COMPOSE_TOOL_NAME,
            description=(
                "Compose multiple skills into a sequential workflow. "
                "Each step runs the specified skill with its args; the "
                "previous step's result is injected as ``_prev_result`` "
                "so later steps can adapt. Steps execute serially — "
                "if any step fails, composition stops and the error is "
                "returned alongside the partial trace."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["workflow"],
                "properties": {
                    "workflow": {
                        "type": "array",
                        "description": (
                            "Ordered list of skill invocations. Each item "
                            "must have 'skill_id' and optionally 'args'."
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["skill_id"],
                            "properties": {
                                "skill_id": {
                                    "type": "string",
                                    "description": "Skill ID to invoke",
                                },
                                "args": {
                                    "type": "object",
                                    "description": "Arguments passed to the skill",
                                },
                            },
                        },
                    },
                },
            },
        )

    async def _invoke_compose(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        args = dict(call.args or {})
        workflow = args.get("workflow")
        if not isinstance(workflow, list) or not workflow:
            return self._error_result(
                call.id,
                "skill_compose requires a non-empty 'workflow' array",
                t0,
            )

        trace: list[dict[str, Any]] = []
        _ctx = SkillContext(_registry=self._registry)
        import inspect as _inspect

        for idx, step in enumerate(workflow):
            if not isinstance(step, dict):
                return self._error_result(
                    call.id,
                    f"workflow[{idx}] must be an object",
                    t0,
                )
            skill_id = step.get("skill_id")
            if not isinstance(skill_id, str) or not skill_id.strip():
                return self._error_result(
                    call.id,
                    f"workflow[{idx}] missing 'skill_id'",
                    t0,
                )
            step_args = dict(step.get("args") or {})
            # Inject previous result so steps can pipe outputs.
            if trace:
                step_args["_prev_result"] = trace[-1].get("result")

            try:
                skill = self._registry.get(skill_id)
            except UnknownSkillError as exc:
                trace.append({
                    "step": idx, "skill_id": skill_id,
                    "ok": False, "error": str(exc),
                })
                return ToolResult(
                    call_id=call.id, ok=False,
                    content={"trace": trace, "stopped_at": idx},
                    error=f"skill_compose: step {idx}: {exc}",
                    latency_ms=self._elapsed_ms(t0),
                )

            _sig = _inspect.signature(skill.run)
            _has_ctx = "ctx" in _sig.parameters
            try:
                if _has_ctx:
                    out = await skill.run(
                        SkillInput(args=step_args), ctx=_ctx,
                    )
                else:
                    out = await skill.run(SkillInput(args=step_args))
            except Exception as exc:  # noqa: BLE001
                trace.append({
                    "step": idx, "skill_id": skill_id,
                    "ok": False, "error": f"{type(exc).__name__}: {exc}",
                })
                return ToolResult(
                    call_id=call.id, ok=False,
                    content={"trace": trace, "stopped_at": idx},
                    error=(
                        f"skill_compose: step {idx} "
                        f"({skill_id}): {type(exc).__name__}: {exc}"
                    ),
                    latency_ms=self._elapsed_ms(t0),
                )

            trace.append({
                "step": idx, "skill_id": skill_id,
                "ok": bool(out.ok), "result": out.result,
            })
            if not out.ok:
                return ToolResult(
                    call_id=call.id, ok=False,
                    content={"trace": trace, "stopped_at": idx},
                    error=(
                        f"skill_compose: step {idx} "
                        f"({skill_id}) returned ok=False"
                    ),
                    latency_ms=self._elapsed_ms(t0),
                )

        return ToolResult(
            call_id=call.id, ok=True,
            content={"trace": trace, "steps": len(trace)},
            error=None, latency_ms=self._elapsed_ms(t0),
        )

    # ── Epic #27 G-04: progressive-disclosure run dispatcher ───────

    def _run_spec(self) -> ToolSpec:
        """ToolSpec for ``skill_run`` — the unified invocation path.

        Always exposed so the description can lean on it. In
        ``unified`` mode this is the ONLY way to invoke a skill; in
        ``inline`` mode it's an alias that coexists with the per-skill
        ``skill_<id>`` tools.
        """
        mode = self._effective_disclosure_mode()
        if mode == DISCLOSURE_MODE_UNIFIED:
            description = (
                "Invoke a registered skill by id. This is the ONLY way "
                "to run a skill in the current disclosure mode — "
                "per-skill ``skill_<id>`` tools are not exposed. "
                "Discovery flow:\n"
                "  1. ``skill_browse(query)`` — find candidate ids.\n"
                "  2. ``skill_view(skill_id)`` — read SKILL.md to see "
                "what args the skill expects.\n"
                "  3. ``skill_run(skill_id, args={...})`` — invoke.\n"
                "Returns whatever the skill returned in its "
                "SkillOutput.result; on skill failure surfaces "
                "ok=False with an error string."
            )
        else:
            description = (
                "Invoke a registered skill by id. Equivalent to calling "
                "``skill_<skill_id>`` directly — same code path, same "
                "variant selection, same error coercion. Useful when "
                "you've just discovered a skill via ``skill_browse`` "
                "and want to call it without waiting for the per-skill "
                "tool to surface on the next turn."
            )
        return ToolSpec(
            name=META_RUN_TOOL_NAME,
            description=description,
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": (
                            "Registered skill id (e.g. "
                            "``demo.read_and_summarize``). Discover "
                            "available ids with ``skill_browse`` or "
                            "``skill_status``."
                        ),
                    },
                    "args": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Arguments forwarded to "
                            "Skill.run(SkillInput(args=...)). Read the "
                            "skill's SKILL.md (via ``skill_view``) to "
                            "see what keys it expects. Omit / pass "
                            "``{}`` for skills that take no args."
                        ),
                    },
                },
            },
        )

    # ── Wave-27 fix-LAT7: skill_install / skill_uninstall ──────────

    def _install_spec(self) -> ToolSpec:
        """ToolSpec for the always-exposed ``skill_install`` meta-tool.

        Lets the agent clone a GitHub-hosted skill into
        ``~/.xmclaw/skills_user/<id>/`` and register it. Reuses
        :func:`xmclaw.skills.marketplace.install_from_source` so the
        same safety nets (structure check + skill_scanner) apply as
        the CLI's ``xmclaw skill install`` path. Trust tier is marked
        "manual" on the install record.
        """
        return ToolSpec(
            name=META_INSTALL_TOOL_NAME,
            description=(
                "Install a skill into ``~/.xmclaw/skills_user/<skill_id>/`` "
                "and register it. The agent calls this when "
                "``skill_browse`` surfaced a promising third-party "
                "skill, or when the user says 'install <repo>'. "
                "``source`` accepts: ``github:owner/repo``, "
                "``git+https://...``, ``https://....git``, OR a local "
                "filesystem path (Windows ``C:\\...``, POSIX ``/...``, "
                "or ``file://...`` URL) when the skill is already on "
                "disk. SKILL.md-only repos (Claude Code / Cursor / "
                "skills.sh style) ARE supported — the loader wraps "
                "them as MarkdownProcedureSkill; no manifest.json / "
                "skill.py needed. Skill is picked up by the daemon's "
                "UserSkillsLoader on the next boot (or immediately "
                "via skills_watcher). Returns ``{skill_id, install_"
                "path, findings}``. "
                "**MCP servers are also supported** (2026-06-07): if the "
                "repo is an MCP server (package.json/pyproject with MCP "
                "markers, e.g. ``*-mcp-server``) rather than a skill, it is "
                "auto-registered into ``config.mcp_servers`` and hot-loaded "
                "into the running MCP hub — its tools appear next turn with "
                "no restart. Return then has ``kind:'mcp'`` + ``register`` "
                "status. Only a repo that is NEITHER a skill NOR an MCP "
                "server is rejected (with an actionable error). "
                "Idempotent: re-installing the same id wipes the "
                "previous copy first (upgrade)."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["source"],
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Where to install from. Examples: "
                            "``github:owner/repo``, "
                            "``https://github.com/foo/bar.git``, "
                            "``C:\\\\Users\\\\me\\\\my-skill`` "
                            "(local Windows path), "
                            "``/home/me/my-skill`` (local POSIX)."
                        ),
                    },
                    "skill_id": {
                        "type": "string",
                        "description": (
                            "Optional install-id override. Default: "
                            "derived from the URL's last path "
                            "segment (``.git`` stripped, slug-cased)."
                        ),
                    },
                },
            },
        )

    def _uninstall_spec(self) -> ToolSpec:
        return ToolSpec(
            name=META_UNINSTALL_TOOL_NAME,
            description=(
                "Remove a previously-installed skill from "
                "``~/.xmclaw/skills_user/<id>/`` and unregister it. "
                "Use when a skill turns out to be broken or not "
                "useful. Returns ``{removed: bool}``. Idempotent — "
                "removing an unknown id returns ``removed=false`` "
                "without raising."
            ),
            parameters_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill_id to uninstall.",
                    },
                },
            },
        )

    async def _invoke_install(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """Run the install pipeline. Wrapped in ``asyncio.to_thread``
        because :func:`marketplace.install_from_source` does sync git
        clone + filesystem I/O + security scan — keeping it on the
        event loop would block other tool calls for several seconds.
        """
        import asyncio
        args = dict(call.args or {})
        source = args.get("source")
        if not isinstance(source, str) or not source.strip():
            return self._error_result(
                call.id,
                "skill_install requires a non-empty 'source' string",
                t0,
            )
        skill_id = args.get("skill_id")
        if skill_id is not None and not isinstance(skill_id, str):
            skill_id = None
        try:
            from xmclaw.skills.marketplace import (
                MarketplaceError, install_from_source,
            )
            result = await asyncio.to_thread(
                install_from_source,
                source.strip(),
                skill_id=skill_id.strip() if skill_id else None,
                block_critical=self._block_critical_installs,
            )
        except MarketplaceError as exc:
            return self._error_result(
                call.id, f"install failed: {exc}", t0,
            )
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                call.id,
                f"install crashed ({type(exc).__name__}): {exc}",
                t0,
            )

        # ── MCP server：登记进 config.mcp_servers + 热加载（2026-06-07）──
        if getattr(result, "kind", "skill") == "mcp" and result.mcp_config:
            reg = await _register_and_hotload_mcp(result.skill_id, result.mcp_config)
            if reg.get("hot_loaded"):
                note = (
                    f"识别为 MCP server，已登记并**热加载**（status=connected），其工具"
                    f"下一轮即可用，无需重启。{result.mcp_config.get('_note','')}"
                )
            elif reg.get("persisted"):
                note = (
                    f"识别为 MCP server，已写入 config.mcp_servers['{result.skill_id}']，"
                    f"但热加载未连上（status={reg.get('status')}）。多半是启动命令需微调或"
                    f"缺 Node/uv 运行时。{result.mcp_config.get('_note','')} "
                    f"改好命令后重启 daemon 即生效。"
                    + (f" [{reg['error']}]" if reg.get("error") else "")
                )
            else:
                note = (
                    f"识别为 MCP server 但自动登记失败（{reg.get('error','no config context')}）。"
                    f"请手动把下面加进 daemon/config.json 的 mcp_servers['{result.skill_id}']："
                    f"{result.mcp_config}"
                )
            return ToolResult(
                call_id=call.id,
                ok=True,
                content={
                    "skill_id": result.skill_id,
                    "kind": "mcp",
                    "install_path": str(result.install_path),
                    "mcp_config": result.mcp_config,
                    "register": reg,
                    "findings": result.findings,
                    "note": note,
                },
                error=None,
                latency_ms=self._elapsed_ms(t0),
                side_effects=(str(result.install_path),),
            )

        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "skill_id": result.skill_id,
                "kind": "skill",
                "install_path": str(result.install_path),
                "version": result.version,
                "source": result.source,
                "findings": result.findings,
                "note": (
                    "Skill installed. The daemon's skills_watcher "
                    "should pick it up within seconds; "
                    "skill_<id>-shaped tool will appear in your next "
                    "tool list. Call ``skill_browse`` to confirm."
                ),
            },
            error=None,
            latency_ms=self._elapsed_ms(t0),
            side_effects=(str(result.install_path),),
        )

    def _invoke_uninstall(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        args = dict(call.args or {})
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            return self._error_result(
                call.id,
                "skill_uninstall requires 'skill_id'",
                t0,
            )
        try:
            from xmclaw.skills.marketplace import remove
            removed = remove(skill_id.strip())
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                call.id,
                f"uninstall failed ({type(exc).__name__}): {exc}",
                t0,
            )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "skill_id": skill_id.strip(),
                "removed": removed,
                "note": (
                    "If removed=true, the skill_<id> tool will "
                    "disappear from your next tool list. If false, "
                    "the id wasn't in the installed-registry — it "
                    "may have been registered by a different code "
                    "path (built-in skill, ~/.agents/skills/, etc.) "
                    "and skill_uninstall doesn't touch those."
                ),
            },
            error=None,
            latency_ms=self._elapsed_ms(t0),
        )

    # ── registry-backed skill spec/invoke ──────────────────────────

    def _spec_for(self, skill_id: str) -> ToolSpec | None:
        try:
            ref = self._registry.ref(skill_id)
        except UnknownSkillError:
            return None

        manifest = ref.manifest
        description = self._build_description(skill_id, manifest, ref.version)
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Arguments forwarded to Skill.run(SkillInput(args=...)). "
                "Pass whatever fields the skill's run() expects."
            ),
        }
        # Epic #27 G-05 (2026-05-19): stash conditional-activation
        # glob list under ``x_paths`` so the prefilter can boost / gate
        # this skill against the agent's recent file-op paths. JSON-
        # schema's ``x_`` extension namespace keeps this invisible to
        # provider wire-formatters that strict-validate the schema.
        if manifest.paths:
            schema["x_paths"] = list(manifest.paths)
        # Same trick for triggers — already read by the prefilter token
        # matcher, just hadn't been wired through tool_bridge before.
        # Kept here so the LLM-facing description doesn't have to carry
        # a redundant copy.
        if manifest.triggers:
            schema["x_triggers"] = list(manifest.triggers)
        return ToolSpec(
            name=_to_tool_name(skill_id),
            description=description,
            parameters_schema=schema,
        )

    def _build_description(
        self, skill_id: str, manifest, version: int
    ) -> str:
        """Assemble the human-readable description for one tool spec.

        Ordering (B-176 → B-177):
          1. Body description (the meat — what it DOES)
          2. "Use when ..." trigger hints
          3. Minimal id / provenance trailer (Epic #27 G-06: adds
             trust level so the LLM can read the skill's
             provenance class without needing skill_status / view)
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

        # Trailer: minimal id reference + provenance (compact, not a
        # headline). Epic #27 G-06: trust + (optionally) allowed_tools
        # so the LLM has at least the metadata to recognise a
        # capability-restricted skill before invoking it.
        trust = getattr(manifest, "trust_level", None)
        trust_part = ""
        if trust is not None:
            trust_val = getattr(trust, "value", trust)
            trust_part = f", trust={trust_val}"
        parts.append(
            f"[skill:{skill_id} v{version}{trust_part}, "
            f"by={manifest.created_by}]"
        )

        if getattr(manifest, "allowed_tools", None):
            tools_preview = ", ".join(manifest.allowed_tools[:5])
            more = (
                f" (+{len(manifest.allowed_tools) - 5} more)"
                if len(manifest.allowed_tools) > 5 else ""
            )
            parts.append(f"allowed_tools: {tools_preview}{more}")

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

    def _invalidate_tool_name_cache(self) -> None:
        """Drop the lazy cache so the next invoke rebuilds it.

        Called by list_tools() so that any registry mutation (new skill
        registration, promote, rollback, uninstall) is visible on the
        next turn — otherwise list_tools() shows the new skill but
        invoke() returns "unknown skill tool" because the stale cache
        doesn't contain it.
        """
        self._tool_name_cache = None

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
