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
META_INSTALL_TOOL_NAME = "skill_install"
META_UNINSTALL_TOOL_NAME = "skill_uninstall"
# Epic #27 P0 G-01 (2026-05-19) — introspection tools so the agent can
# self-diagnose "did my install work?" / "what's in this skill?" instead
# of running list_dir + bash in circles.
META_STATUS_TOOL_NAME = "skill_status"
META_VIEW_TOOL_NAME = "skill_view"


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
        watcher: Any = None,
    ) -> None:
        self._registry = registry
        self._description_prefix = description_prefix
        self._tool_name_cache: dict[str, str] | None = None
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
        if call.name == META_INSTALL_TOOL_NAME:
            return await self._invoke_install(call, t0)
        if call.name == META_UNINSTALL_TOOL_NAME:
            return self._invoke_uninstall(call, t0)
        if call.name == META_STATUS_TOOL_NAME:
            return self._invoke_status(call, t0)
        if call.name == META_VIEW_TOOL_NAME:
            return self._invoke_view(call, t0)

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
                "load_failures": load_failures,
                "pending_restarts": pending_restarts,
                "notes": notes,
            },
            error=None,
            latency_ms=self._elapsed_ms(t0),
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
            canonical, extras = (_Path.home() / ".xmclaw/skills_user", [])
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
                "path, findings}``; raises if the dir has none of "
                "manifest.json / SKILL.md / skill.py, or if the "
                "security scanner flags a CRITICAL finding. "
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
        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "skill_id": result.skill_id,
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
