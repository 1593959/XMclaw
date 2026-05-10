"""Built-in tools -- file_read, file_write, list_dir, bash, web_fetch, web_search.

Posture: a local AI assistant the user deliberately installed gets
full user-level access by default. The optional ``allowed_dirs`` arg is
for sandboxed test / demo setups; in normal use it's None and fs tools
have the same access the invoking process does.

Tool families and their kill-switches:

  * filesystem (file_read, file_write, list_dir)
      Always on. The only guard is ``allowed_dirs`` (opt-in sandbox).
  * bash  -- toggled by ``enable_bash`` (default True)
      Runs a shell command via subprocess with a timeout; captures
      stdout + stderr. Use at your own risk; this is the "I trust my
      local agent" posture the user asked for.
  * web_fetch, web_search -- toggled by ``enable_web`` (default True)
      ``web_fetch`` GETs a URL and returns its text (truncated).
      ``web_search`` uses DuckDuckGo's HTML endpoint -- no API key
      required, low-quality but always available.

All tools return ``ToolResult`` with ``ok=True`` and a string ``content``
on success, or ``ok=False`` with a human-readable ``error`` on failure.
The agent loop now renders failures as ``"ERROR: <error>"`` in the
tool-message content so the LLM sees the real reason instead of "None".
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider

# B-324: ToolSpec definitions split into ``_specs`` (pure data) and
# bullet/dedup/persona/web helpers split into ``_helpers``. Re-imported
# here under their original module-level names so external callers
# (post_sampling_hooks.py, profiles router, memory router, tests, …)
# keep working without import-path churn.
#
# ``X as X`` form is the PEP-484 / mypy / pyright pattern for "this
# is an explicit re-export". Without it mypy --strict (and the test
# typing pass) reports ``does not explicitly export attribute X``
# for every external ``from xmclaw.providers.tool.builtin import X``
# call site. The aliasing tells the type checker the import was
# intentional public API, not an internal detail.
#
# ``noqa: F401`` covers the ones not used inside *this* module.
from xmclaw.providers.tool._specs import (  # noqa: F401
    _AGENT_STATUS_SPEC as _AGENT_STATUS_SPEC,
    _APPLY_PATCH_SPEC as _APPLY_PATCH_SPEC,
    _ASK_USER_QUESTION_SPEC as _ASK_USER_QUESTION_SPEC,
    _BASH_SPEC as _BASH_SPEC,
    _CURRICULUM_LIST_SPEC as _CURRICULUM_LIST_SPEC,
    _ENTER_WORKTREE_SPEC as _ENTER_WORKTREE_SPEC,
    _EXIT_WORKTREE_SPEC as _EXIT_WORKTREE_SPEC,
    _FILE_DELETE_SPEC as _FILE_DELETE_SPEC,
    _FILE_READ_SPEC as _FILE_READ_SPEC,
    _FILE_WRITE_SPEC as _FILE_WRITE_SPEC,
    _GLOB_FILES_SPEC as _GLOB_FILES_SPEC,
    _GREP_FILES_SPEC as _GREP_FILES_SPEC,
    _JOURNAL_APPEND_SPEC as _JOURNAL_APPEND_SPEC,
    _JOURNAL_RECALL_SPEC as _JOURNAL_RECALL_SPEC,
    _LEARN_ABOUT_USER_SPEC as _LEARN_ABOUT_USER_SPEC,
    _LIST_DIR_SPEC as _LIST_DIR_SPEC,
    _MEMORY_COMPACT_SPEC as _MEMORY_COMPACT_SPEC,
    _MEMORY_PIN_SPEC as _MEMORY_PIN_SPEC,
    _MEMORY_SEARCH_SPEC as _MEMORY_SEARCH_SPEC,
    _NOTE_WRITE_SPEC as _NOTE_WRITE_SPEC,
    _PROPOSE_CURRICULUM_EDIT_SPEC as _PROPOSE_CURRICULUM_EDIT_SPEC,
    _RECALL_USER_PREFS_SPEC as _RECALL_USER_PREFS_SPEC,
    _REMEMBER_SPEC as _REMEMBER_SPEC,
    _SCHEDULE_FOLLOWUP_SPEC as _SCHEDULE_FOLLOWUP_SPEC,
    _SQLITE_QUERY_SPEC as _SQLITE_QUERY_SPEC,
    _TODO_READ_SPEC as _TODO_READ_SPEC,
    _TODO_WRITE_SPEC as _TODO_WRITE_SPEC,
    _UPDATE_PERSONA_SPEC as _UPDATE_PERSONA_SPEC,
    _VOICE_SYNTHESIZE_SPEC as _VOICE_SYNTHESIZE_SPEC,
    _VOICE_TRANSCRIBE_SPEC as _VOICE_TRANSCRIBE_SPEC,
    _WEB_FETCH_SPEC as _WEB_FETCH_SPEC,
    _WEB_SEARCH_SPEC as _WEB_SEARCH_SPEC,
)
from xmclaw.providers.tool._helpers import (  # noqa: F401
    PERSONA_CHAR_CAPS as PERSONA_CHAR_CAPS,
    _PERSONA_BASENAMES_LOOKUP as _PERSONA_BASENAMES_LOOKUP,
    _append_under_section as _append_under_section,
    _bullet_core as _bullet_core,
    _bullet_token_set as _bullet_token_set,
    _fail as _fail,
    _is_fuzzy_duplicate as _is_fuzzy_duplicate,
    _parse_ddg_html as _parse_ddg_html,
    collapse_existing_duplicates as collapse_existing_duplicates,
    enforce_char_cap as enforce_char_cap,
)


# B-233: lowered from 200_000 to 50_000. With chars/4 estimate that's
# ~12K tokens per fetch — still useful for normal pages, but no longer
# enough that two fetches push a kimi-k2.6 session past its 262K limit
# (real-data: chat-18e1711d hit the wall on hop 15 because earlier
# fetches dumped HTML lists ~150K each into history). Callers can
# raise per-call via the ``max_chars`` arg when they really need more.
_MAX_WEB_BYTES = 50_000
_BASH_DEFAULT_TIMEOUT = 30.0
_BASH_MAX_OUTPUT = 100_000
_VALID_TODO_STATUSES = {"pending", "in_progress", "done"}

# B-92: cross-boundary store for in-flight ``ask_user_question`` calls.
# The tool handler awaits a Future stored here; the daemon's WS
# handler resolves it when the user clicks an answer in the UI. Keys
# are uuid4 hex; values are ``asyncio.Future``. Cleared by the tool
# handler's ``finally`` block whether the future resolved or timed
# out, so the dict never accumulates dead entries.
_PENDING_QUESTIONS: dict[str, asyncio.Future] = {}


# B-94: process-wide memo of the workspace path each currently-active
# worktree was originally entered from. Keyed by the worktree's
# absolute path; value is the original root path. Lets ``exit_worktree``
# walk back to where ``enter_worktree`` started, even when the agent
# left the worktree open across many turns.
_WORKTREE_ORIGIN: dict[str, Path] = {}


# B-99: snapshot of question metadata, indexed by question_id, used
# by the WS reconnect path (``GET /api/v2/pending_questions``) to
# rebuild the QuestionCard after a browser refresh. Cleared whenever
# ``_PENDING_QUESTIONS`` removes the future, so the two stay in sync.
_PENDING_QUESTION_PAYLOADS: dict[str, dict] = {}


def list_pending_questions() -> list[dict]:
    """B-99: return snapshots of every in-flight question. Each entry
    has the same shape as the AGENT_ASKED_QUESTION event payload so
    the front-end can rebuild the QuestionCard without a special
    code path."""
    return list(_PENDING_QUESTION_PAYLOADS.values())


def resolve_pending_question(
    question_id: str, answer: "str | list[str]",
) -> bool:
    """Resolve an in-flight ``ask_user_question`` future.

    Called from the daemon's WS handler when the client sends an
    ``answer_question`` frame. Returns True when the future was
    resolved (i.e. the question was actually pending), False when
    the question id was unknown or already resolved (stale answer
    after a timeout, double-click, etc).
    """
    fut = _PENDING_QUESTIONS.get(question_id)
    if fut is None or fut.done():
        return False
    fut.set_result(answer)
    return True

class BuiltinTools(ToolProvider):
    """Local filesystem, shell, and web tools.

    Parameters
    ----------
    allowed_dirs : list[Path | str] | None
        Optional sandbox. If provided, all filesystem tools refuse paths
        outside these directories. None (default) means no sandbox --
        the tools have whatever access the running process has.
    enable_bash : bool
        If False, ``bash`` returns a structured refusal. Default True.
    enable_web : bool
        If False, ``web_fetch`` and ``web_search`` refuse. Default True.
    """

    def __init__(
        self,
        allowed_dirs: list[Path | str] | None = None,
        *,
        enable_bash: bool = True,
        enable_web: bool = True,
        todo_listener: "object | None" = None,
        workspace_root_provider: "object | None" = None,
        workspace_manager_provider: "object | None" = None,
        persona_dir_provider: "object | None" = None,
        persona_writeback: "object | None" = None,
        persona_store_provider: "object | None" = None,
        memory_manager: "object | None" = None,
        embedder: "object | None" = None,
        # B-388 (Sprint 2): optional STT / TTS providers. When wired,
        # ``voice_transcribe`` / ``voice_synthesize`` are advertised on
        # list_tools. Each is gated independently (you can ship a
        # transcribe-only or synthesize-only setup). Providers are duck-
        # typed: STT must have ``async transcribe(bytes) -> str``; TTS
        # must have ``async synthesize(text, voice) -> bytes``.
        stt_provider: "object | None" = None,
        tts_provider: "object | None" = None,
    ) -> None:
        self._allowed = (
            [Path(d).resolve() for d in allowed_dirs] if allowed_dirs else None
        )
        self._enable_bash = enable_bash
        self._enable_web = enable_web
        # Optional callable () -> Path returning the active persona profile
        # directory (e.g. ~/.xmclaw/persona/profiles/default/). The
        # ``remember`` and ``learn_about_user`` tools target MEMORY.md and
        # USER.md inside this directory. Without a provider, the tools
        # fall back to ``~/.xmclaw/persona/profiles/default/``.
        self._persona_dir_provider = persona_dir_provider
        # Optional callable invoked AFTER a successful persona-file write,
        # so the daemon can rebuild ``app.state.agent._system_prompt``
        # immediately and the agent picks up its own update on the next
        # turn (no daemon restart needed). Signature: ``(basename) -> None``.
        self._persona_writeback = persona_writeback
        # B-198 Phase 3: optional callable () -> PersonaStore | None.
        # When wired, persona-mutating tools (``update_persona`` /
        # ``remember`` / ``memory_pin`` / ``learn_about_user``) route
        # writes through the store (DB-as-truth) instead of touching
        # markdown directly. Render-to-disk happens inside the store
        # so external readers see the new state immediately. Without
        # the provider these tools fall back to legacy markdown writes.
        self._persona_store_provider = persona_store_provider
        # Optional callable () -> Path | None returning the daemon's
        # active workspace root (driven by ~/.xmclaw/state.json via
        # WorkspaceManager). When the LLM omits an explicit `cwd` arg
        # on a bash call we fall back to this so commands like `ls` /
        # `pwd` run inside the project the user is actually working
        # on, not wherever the daemon was started from.
        self._workspace_root_provider = workspace_root_provider
        # B-331: callable () -> WorkspaceManager | None for the
        # write-path containment audit. When wired, every file_write /
        # apply_patch logs a WARNING + emits a security event when the
        # target is outside every configured workspace root. Pre-B-331
        # WorkspaceManager.resolve_path_to_root had zero callers — the
        # docstring promised "used by tools to gate writes" but no
        # tool actually consulted it. Visibility-only: writes still
        # succeed (anti-req #5 sandbox is a separate epic, needs UX
        # design for ASK-confirm vs deny). The signal is enough so an
        # operator reviewing daemon.log can spot the agent escaping
        # the configured workspace.
        self._workspace_manager_provider = workspace_manager_provider
        # Per-session todo lists. Key: session_id (falls back to "_default"
        # when a caller doesn't fill in ToolCall.session_id).
        self._todos: dict[str, list[dict[str, str]]] = {}
        # Optional callback fired on every todo_write so the agent loop /
        # daemon can emit a TODO_UPDATED event to the bus. Signature:
        # ``def todo_listener(session_id, items) -> None``. Keeping it as
        # a plain callable avoids coupling this module to the bus type.
        self._todo_listener = todo_listener
        # B-63: per-path async locks live in xmclaw.utils.fs_locks
        # (B-65 promoted them to a shared module-level store so
        # DreamCompactor and BuiltinFileMemoryProvider lock the
        # SAME mutex per file).
        # B-40: optional MemoryManager handle so the unified
        # ``memory_search`` tool can fan a query across every wired
        # memory provider (builtin file + sqlite_vec / hindsight /
        # supermemory / mem0). When None, memory_search is hidden
        # from list_tools — the agent doesn't get a tool that can't
        # do anything useful.
        self._memory_manager = memory_manager
        # B-42: optional embedder so memory_search embeds the query
        # and gets real semantic hits back. Without it, the tool falls
        # through to MemoryManager.query's keyword path — same as before
        # B-41/B-42, just less useful. Wired post-construction by the
        # factory because EmbeddingProvider is built alongside the
        # indexer (after BuiltinTools).
        self._embedder = embedder
        # B-388: voice provider handles. Each is advertised on
        # list_tools when wired, so a daemon without faster-whisper /
        # edge-tts installed simply doesn't expose those tools.
        self._stt_provider = stt_provider
        self._tts_provider = tts_provider

    def set_voice_providers(
        self,
        stt: "object | None" = None,
        tts: "object | None" = None,
    ) -> None:
        """B-388: wire voice providers AFTER construction.

        Symmetric with :meth:`set_memory_manager` / :meth:`set_embedder`
        — the daemon factory may need to wire voice providers AFTER
        BuiltinTools was built (e.g. when ``config_watcher`` hot-reloads
        the ``voice`` block). Pass ``None`` to clear; pass an instance
        to replace.
        """
        self._stt_provider = stt
        self._tts_provider = tts

    def set_memory_manager(self, mgr: "object | None") -> None:
        """Wire (or clear) the MemoryManager AFTER construction.

        BuiltinTools is built before the MemoryManager in
        ``factory.py``, so the manager has to be patched in
        post-construction. Surfaces / hides ``memory_search`` from
        ``list_tools`` accordingly.
        """
        self._memory_manager = mgr

    def set_embedder(self, embedder: "object | None") -> None:
        """B-42: wire (or clear) the EmbeddingProvider post-construction.

        When set, ``memory_search`` embeds the query and routes through
        the vector path. When None, the tool keyword-searches.
        """
        self._embedder = embedder

    def list_tools(self) -> list[ToolSpec]:
        specs = [
            _FILE_READ_SPEC, _FILE_WRITE_SPEC, _APPLY_PATCH_SPEC,
            _LIST_DIR_SPEC, _GLOB_FILES_SPEC, _GREP_FILES_SPEC,
            _FILE_DELETE_SPEC,
        ]
        if self._enable_bash:
            specs.append(_BASH_SPEC)
        if self._enable_web:
            specs.extend([_WEB_FETCH_SPEC, _WEB_SEARCH_SPEC])
        specs.extend([_TODO_WRITE_SPEC, _TODO_READ_SPEC])
        # Self-modifying memory tools are gated by the persona_dir
        # provider — without it we have nowhere to write, so we don't
        # advertise the tools. Tests construct BuiltinTools without
        # the provider; production wiring (factory.py) supplies it.
        if self._persona_dir_provider is not None:
            specs.extend([_REMEMBER_SPEC, _LEARN_ABOUT_USER_SPEC, _UPDATE_PERSONA_SPEC])
        # B-200 / Phase 5: curriculum self-edit proposal tools. Gated
        # on persona_store_provider — the proposal storage uses the
        # same memory.db, so without it we'd have nowhere to queue.
        if self._persona_store_provider is not None:
            specs.extend([
                _PROPOSE_CURRICULUM_EDIT_SPEC,
                _CURRICULUM_LIST_SPEC,
            ])
        # schedule_followup is always available — it doesn't need any
        # constructor wiring; the cron store is a process-wide singleton.
        specs.append(_SCHEDULE_FOLLOWUP_SPEC)
        # B-37: read-only SQL access to the agent's own state DBs.
        # Always available — the DBs themselves may not exist yet
        # (fresh install), in which case the tool reports that
        # cleanly.
        specs.append(_SQLITE_QUERY_SPEC)
        # B-40: unified memory_search across every wired provider.
        # Only advertised when a MemoryManager is wired — without one
        # the tool would be a no-op.
        if self._memory_manager is not None:
            specs.append(_MEMORY_SEARCH_SPEC)
        # B-45: agent-facing tools to write to the user's Notes +
        # Journal panels — both are evolution surfaces (workflow notes,
        # lessons learned, daily logs). Path-only ops, always available.
        specs.extend([_NOTE_WRITE_SPEC, _JOURNAL_APPEND_SPEC])
        # Epic #24 Phase 2.5: journal_recall reads past session
        # journals written by JournalWriter so the agent can avoid
        # rediscovering yesterday's mistakes. Always advertised — the
        # handler reports cleanly when the journal dir is empty.
        specs.append(_JOURNAL_RECALL_SPEC)
        # Epic #24 Phase 4.2: recall_user_preferences reads the
        # auto-extracted USER.md section (ProfileExtractor output).
        # Gated on persona_dir wiring — without it the tool has no
        # file to read.
        if self._persona_dir_provider is not None:
            specs.append(_RECALL_USER_PREFS_SPEC)
        # B-49: self-introspection tool. Always advertised — works
        # even with zero providers wired (returns "nothing wired").
        specs.append(_AGENT_STATUS_SPEC)
        # B-92: ask the user a multiple-choice question mid-turn.
        # Always advertised — daemon-process-local resolver works
        # even without persona / memory wiring.
        specs.append(_ASK_USER_QUESTION_SPEC)
        # B-94: free-code parity — let the agent spin up an isolated
        # git worktree for risky / experimental changes. Always
        # advertised; ``enter_worktree`` itself errors out cleanly
        # when not in a git repo (so test contexts aren't surprised).
        specs.extend([_ENTER_WORKTREE_SPEC, _EXIT_WORKTREE_SPEC])
        # B-52: memory_compact triggers an immediate Auto-Dream pass.
        # Always advertised; the handler refuses cleanly when no LLM
        # is wired (which is the only failure mode).
        specs.append(_MEMORY_COMPACT_SPEC)
        # B-53: memory_pin lands in MEMORY.md's `## Pinned` section.
        # Gated on persona_dir wiring (same as ``remember``); the
        # actual write reuses _append_under_section so pinned bullets
        # share dedup behaviour.
        if self._persona_dir_provider is not None:
            specs.append(_MEMORY_PIN_SPEC)
        # B-388: voice tools. Each direction gates independently, so
        # a transcribe-only setup (faster-whisper installed but no
        # edge-tts) advertises voice_transcribe and hides
        # voice_synthesize, and vice versa.
        if self._stt_provider is not None:
            specs.append(_VOICE_TRANSCRIBE_SPEC)
        if self._tts_provider is not None:
            specs.append(_VOICE_SYNTHESIZE_SPEC)
        return specs

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            if call.name == "file_read":
                return await self._file_read(call, t0)
            if call.name == "file_write":
                return await self._file_write(call, t0)
            if call.name == "apply_patch":
                return await self._apply_patch(call, t0)
            if call.name == "list_dir":
                return await self._list_dir(call, t0)
            if call.name == "glob_files":
                return await self._glob_files(call, t0)
            if call.name == "grep_files":
                return await self._grep_files(call, t0)
            if call.name == "file_delete":
                return await self._file_delete(call, t0)
            if call.name == "bash":
                if not self._enable_bash:
                    return _fail(call, t0, "bash tool is disabled in config")
                return await self._bash(call, t0)
            if call.name == "web_fetch":
                if not self._enable_web:
                    return _fail(call, t0, "web tools are disabled in config")
                return await self._web_fetch(call, t0)
            if call.name == "web_search":
                if not self._enable_web:
                    return _fail(call, t0, "web tools are disabled in config")
                return await self._web_search(call, t0)
            if call.name == "todo_write":
                return await self._todo_write(call, t0)
            if call.name == "todo_read":
                return await self._todo_read(call, t0)
            if call.name == "remember":
                if self._persona_dir_provider is None:
                    return _fail(call, t0, "remember tool not configured (no persona dir)")
                return await self._remember(call, t0)
            if call.name == "learn_about_user":
                if self._persona_dir_provider is None:
                    return _fail(call, t0, "learn_about_user tool not configured (no persona dir)")
                return await self._learn_about_user(call, t0)
            if call.name == "update_persona":
                if self._persona_dir_provider is None:
                    return _fail(call, t0, "update_persona tool not configured (no persona dir)")
                return await self._update_persona(call, t0)
            if call.name == "propose_curriculum_edit":
                if self._persona_store_provider is None:
                    return _fail(
                        call, t0,
                        "propose_curriculum_edit not configured (no persona store)",
                    )
                return await self._propose_curriculum_edit(call, t0)
            if call.name == "list_curriculum_proposals":
                if self._persona_store_provider is None:
                    return _fail(
                        call, t0,
                        "list_curriculum_proposals not configured (no persona store)",
                    )
                return await self._list_curriculum_proposals(call, t0)
            if call.name == "schedule_followup":
                return await self._schedule_followup(call, t0)
            if call.name == "sqlite_query":
                return await self._sqlite_query(call, t0)
            if call.name == "memory_search":
                if self._memory_manager is None:
                    return _fail(call, t0, "memory_search not configured (no MemoryManager wired)")
                return await self._memory_search(call, t0)
            if call.name == "note_write":
                return await self._note_write(call, t0)
            if call.name == "journal_append":
                return await self._journal_append(call, t0)
            if call.name == "journal_recall":
                return await self._journal_recall(call, t0)
            if call.name == "recall_user_preferences":
                if self._persona_dir_provider is None:
                    return _fail(
                        call, t0,
                        "recall_user_preferences not configured "
                        "(no persona dir)",
                    )
                return await self._recall_user_preferences(call, t0)
            if call.name == "agent_status":
                return await self._agent_status(call, t0)
            if call.name == "memory_compact":
                return await self._memory_compact(call, t0)
            if call.name == "memory_pin":
                if self._persona_dir_provider is None:
                    return _fail(call, t0, "memory_pin not configured (no persona dir)")
                return await self._memory_pin(call, t0)
            if call.name == "ask_user_question":
                return await self._ask_user_question(call, t0)
            if call.name == "enter_worktree":
                return await self._enter_worktree(call, t0)
            if call.name == "exit_worktree":
                return await self._exit_worktree(call, t0)
            # B-388: voice tools. Each is gated on its provider being
            # wired; without the provider the tool returns a clear
            # "not configured" error pointing at the install hint.
            if call.name == "voice_transcribe":
                if self._stt_provider is None:
                    return _fail(
                        call, t0,
                        "voice_transcribe not configured (no STT provider "
                        "wired — pip install 'xmclaw[voice-stt]' + set "
                        "voice.stt in config)",
                    )
                return await self._voice_transcribe(call, t0)
            if call.name == "voice_synthesize":
                if self._tts_provider is None:
                    return _fail(
                        call, t0,
                        "voice_synthesize not configured (no TTS provider "
                        "wired — pip install 'xmclaw[voice-tts]' + set "
                        "voice.tts in config)",
                    )
                return await self._voice_synthesize(call, t0)
            return _fail(call, t0, f"unknown tool: {call.name!r}")
        except PermissionError as exc:
            return _fail(call, t0, f"permission denied: {exc}")
        except FileNotFoundError as exc:
            return _fail(call, t0, f"file not found: {exc}")
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

    # ── filesystem tools ──────────────────────────────────────────────

    async def _file_read(self, call: ToolCall, t0: float) -> ToolResult:
        """B-57: capped + range-aware file read.

        Three modes, mutually exclusive but resolved by argument
        presence (no explicit mode flag):

        * ``offset`` + ``limit`` set → read line range
        * neither → read up to ``max_bytes`` (default 100KB) from
          the start, append ``[truncated]`` marker if larger
        * Either way: refuse binary-looking files (NUL byte in the
          first 8KB).

        Honors ``allowed_dirs`` sandbox via ``_check_allowed``.
        """
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        path = Path(raw_path)
        self._check_allowed(path)
        if not path.exists():
            return _fail(call, t0, f"file not found: {path}")
        if not path.is_file():
            return _fail(call, t0, f"not a file: {path}")

        # Binary heuristic — read first 8KB raw, look for NUL.
        try:
            with path.open("rb") as fh:
                head = fh.read(8192)
        except OSError as exc:
            return _fail(call, t0, f"open failed: {exc}")
        if b"\x00" in head:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return _fail(
                call, t0,
                f"file looks binary ({size} bytes, NUL byte in first 8KB) "
                f"— file_read is text-only",
            )

        # Range read (offset + limit) takes precedence.
        offset = call.args.get("offset")
        limit = call.args.get("limit")
        if offset is not None or limit is not None:
            try:
                off_i = int(offset) if offset is not None else 1
                lim_i = int(limit) if limit is not None else 2000
            except (TypeError, ValueError):
                return _fail(call, t0, "offset / limit must be integers")
            off_i = max(1, off_i)
            lim_i = max(1, min(lim_i, 50000))
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    lines = []
                    for i, line in enumerate(fh, 1):
                        if i < off_i:
                            continue
                        if len(lines) >= lim_i:
                            break
                        lines.append(line)
            except OSError as exc:
                return _fail(call, t0, f"read failed: {exc}")
            content = "".join(lines)
            return ToolResult(
                call_id=call.id, ok=True, content=content,
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # Byte-cap mode.
        try:
            max_bytes = int(call.args.get("max_bytes") or 100_000)
        except (TypeError, ValueError):
            max_bytes = 100_000
        max_bytes = max(1024, min(max_bytes, 1_000_000))
        try:
            stat = path.stat()
        except OSError as exc:
            return _fail(call, t0, f"stat failed: {exc}")
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                # Read max_bytes worth — actually char-count not byte
                # since Python decodes. Close enough — UTF-8 char
                # length and byte length are equal for ASCII, ≤4x
                # for CJK; we err on the side of slightly more.
                content = fh.read(max_bytes)
        except OSError as exc:
            return _fail(call, t0, f"read failed: {exc}")
        if stat.st_size > max_bytes:
            content += f"\n\n[truncated, {stat.st_size} total bytes; pass max_bytes or offset/limit for more]"
        return ToolResult(
            call_id=call.id, ok=True, content=content,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _file_write(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        text = call.args.get("content")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(text, str):
            return _fail(
                call, t0,
                f"'content' must be string, got {type(text).__name__}",
            )
        path = Path(raw_path)
        self._check_allowed(path)
        # B-331: visibility signal when the write escapes the
        # configured workspace roots. Doesn't block — sandboxing is
        # a separate UX-design epic.
        self._audit_workspace_containment(path, op="file_write")
        path.parent.mkdir(parents=True, exist_ok=True)
        from xmclaw.utils.fs_locks import atomic_write_text
        atomic_write_text(path, text)
        # Structured dict for graders and the bus; agent_loop renders
        # it into a readable tool-message string when feeding to the LLM.
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "path": str(path),
                "bytes": len(text.encode("utf-8")),
            },
            side_effects=(str(path.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _apply_patch(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        edits = call.args.get("edits")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(edits, list) or not edits:
            return _fail(call, t0, "'edits' must be a non-empty list")

        # Pre-validate every edit's shape before touching disk.
        clean: list[tuple[str, str]] = []
        for i, e in enumerate(edits):
            if not isinstance(e, dict):
                return _fail(call, t0, f"edits[{i}] must be an object")
            old_text = e.get("old_text")
            new_text = e.get("new_text")
            if not isinstance(old_text, str) or old_text == "":
                return _fail(call, t0, f"edits[{i}].old_text must be a non-empty string")
            if not isinstance(new_text, str):
                return _fail(call, t0, f"edits[{i}].new_text must be a string")
            clean.append((old_text, new_text))

        path = Path(raw_path)
        self._check_allowed(path)
        # B-331: same workspace-containment audit as file_write.
        self._audit_workspace_containment(path, op="apply_patch")
        if not path.exists() or not path.is_file():
            return _fail(call, t0, f"file does not exist: {path}")
        original = path.read_text(encoding="utf-8")
        text = original

        # Apply edits sequentially. Each old_text must occur exactly once
        # in the *current* text (after prior edits) — so two edits whose
        # search strings overlap are caught here, not silently mis-applied.
        for i, (old_text, new_text) in enumerate(clean):
            count = text.count(old_text)
            if count == 0:
                # B-397 (Sprint 1 stragglers): pre-fix, the error said
                # "file may have changed; re-read it before patching" —
                # the right hint, but real-world LLMs ignored it and
                # repeated the same stale-text edit until max_hops fired
                # (real example: xmclaw-architecture-redesign.md, 40
                # hops, all the same edit). Surface the CURRENT file
                # content + a fuzzy-match suggestion in the error so
                # the LLM has the fresh state inline and can rebase
                # without another file_read round-trip.
                hint = self._stale_match_hint(text, old_text)
                return _fail(
                    call, t0,
                    f"edits[{i}].old_text not found in {path}.\n{hint}",
                )
            if count > 1:
                return _fail(
                    call, t0,
                    f"edits[{i}].old_text occurs {count} times in {path}; "
                    f"include more surrounding context to make it unique",
                )
            text = text.replace(old_text, new_text, 1)

        if text == original:
            return _fail(call, t0, "patch produced no change (every old_text == new_text)")

        # Atomic write: temp + replace so a crash mid-write can't truncate.
        tmp = path.with_suffix(path.suffix + ".patch.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

        before = len(original.encode("utf-8"))
        after = len(text.encode("utf-8"))
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "path": str(path),
                "edits_applied": len(clean),
                "bytes_before": before,
                "bytes_after": after,
                "delta": after - before,
            },
            side_effects=(str(path.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    @staticmethod
    def _stale_match_hint(current_text: str, old_text: str) -> str:
        """B-397: when ``old_text`` doesn't match the current file,
        return a hint that gives the LLM enough context to rebase
        without another file_read round-trip.

        Strategy:
          1. If the file is small (≤ 4000 chars), return the whole thing
             — cheaper than guessing.
          2. Otherwise, find the longest substring of ``old_text`` that
             DOES appear in current_text and return ±10 lines of context
             around it. This handles the common case where the edit's
             anchor is right but a few lines drifted (whitespace, prior
             edit replaced part of the chunk, etc).
          3. If nothing of ``old_text`` matches at all, return the first
             80 lines of current_text — enough for the LLM to recognize
             it's looking at the right file and re-anchor.
        """
        max_inline = 4000
        if len(current_text) <= max_inline:
            return (
                "File may have changed since your last read OR the "
                "old_text is from a stale view. The CURRENT file content "
                "is below — re-base your edit and try again WITHOUT "
                "calling file_read first.\n\n"
                "=== CURRENT FILE ===\n"
                f"{current_text}\n"
                "=== END ==="
            )
        # Search for the longest prefix of old_text that occurs in current.
        # Cheap O(n^2) — old_text is bounded by tool args and current_text
        # is bounded by max_inline check above.
        best_anchor = ""
        for length in range(min(len(old_text), 200), 5, -1):
            sub = old_text[:length]
            if sub in current_text:
                best_anchor = sub
                break
        if best_anchor:
            idx = current_text.index(best_anchor)
            # ±10 lines of context.
            before_lines = current_text[:idx].splitlines()[-10:]
            after_chunk = current_text[idx + len(best_anchor):]
            after_lines = after_chunk.splitlines()[:10]
            ctx_lines = (
                before_lines
                + [best_anchor.rstrip(), "<<<< drifted from here >>>>"]
                + after_lines
            )
            ctx = "\n".join(ctx_lines)
            return (
                "File may have changed; partial match found. Context "
                "around where your old_text WOULD have anchored "
                "(±10 lines):\n\n"
                "=== CONTEXT ===\n"
                f"{ctx}\n"
                "=== END ===\n"
                "Re-base your edit on this context and try again."
            )
        # No partial match — show file head.
        head = "\n".join(current_text.splitlines()[:80])
        return (
            "File may have changed and your old_text doesn't appear at "
            "all. First 80 lines of current file:\n\n"
            "=== HEAD ===\n"
            f"{head}\n"
            "=== END ===\n"
            "Re-anchor your edit and try again WITHOUT calling file_read."
        )

    async def _list_dir(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        pattern = call.args.get("pattern", "*")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(pattern, str) or not pattern:
            pattern = "*"
        try:
            limit = int(call.args.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 5000))
        path = Path(raw_path)
        self._check_allowed(path)
        if not path.exists():
            return _fail(call, t0, f"path does not exist: {path}")
        if not path.is_dir():
            return _fail(call, t0, f"not a directory: {path}")
        # B-58: stream entries one-by-one with an entry-count cap so a
        # huge dir doesn't flood the LLM context. We collect into a
        # list because path.glob's order isn't sorted; sort *all*
        # then truncate vs sort *truncated* — small price for
        # determinism, and a 5000-entry sort is sub-ms.
        try:
            all_entries = sorted(path.glob(pattern))
        except OSError as exc:
            return _fail(call, t0, f"glob failed: {exc}")
        total = len(all_entries)
        truncated = total > limit
        kept = all_entries[:limit]
        lines: list[str] = []
        for entry in kept:
            kind = "l" if entry.is_symlink() else (
                "d" if entry.is_dir() else "f"
            )
            try:
                size = entry.stat().st_size if kind == "f" else 0
            except OSError:
                size = 0
            lines.append(f"{kind} {size:>10} {entry.name}")
        body = "\n".join(lines) if lines else f"(no entries matching {pattern!r})"
        if truncated:
            body += f"\n[truncated, {total - limit} more — pass limit= for all]"
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"{len(lines)} of {total} entries in {path}:\n{body}",
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _glob_files(self, call: ToolCall, t0: float) -> ToolResult:
        """B-46: pure-stdlib glob. Cross-platform — works on Windows
        without needing find / fd / ripgrep installed."""
        pattern = str(call.args.get("pattern") or "").strip()
        if not pattern:
            return _fail(call, t0, "missing 'pattern'")
        root_arg = call.args.get("root")
        root = Path(str(root_arg)) if root_arg else self._cwd_default()
        try:
            root = root.resolve()
        except OSError as exc:
            return _fail(call, t0, f"bad root: {exc}")
        self._check_allowed(root)
        if not root.is_dir():
            return _fail(call, t0, f"not a directory: {root}")
        try:
            limit = int(call.args.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 2000))
        results: list[str] = []
        try:
            # Path.glob handles ``**`` natively when pattern contains it.
            iterator = root.glob(pattern)
            for entry in iterator:
                results.append(str(entry))
                if len(results) >= limit:
                    break
        except (OSError, ValueError) as exc:
            return _fail(call, t0, f"glob failed: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "root": str(root),
                "pattern": pattern,
                "matches": results,
                "count": len(results),
                "truncated": len(results) >= limit,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _grep_files(self, call: ToolCall, t0: float) -> ToolResult:
        """B-46: regex search across files. Pure stdlib re; iterates
        line-by-line so a huge file doesn't OOM. Bounded by max_hits."""
        import re as _re

        pattern = str(call.args.get("pattern") or "")
        if not pattern:
            return _fail(call, t0, "missing 'pattern'")
        glob_pat = str(call.args.get("glob") or "**/*")
        root_arg = call.args.get("root")
        root = Path(str(root_arg)) if root_arg else self._cwd_default()
        try:
            root = root.resolve()
        except OSError as exc:
            return _fail(call, t0, f"bad root: {exc}")
        self._check_allowed(root)
        if not root.is_dir():
            return _fail(call, t0, f"not a directory: {root}")
        try:
            max_hits = int(call.args.get("max_hits") or 200)
        except (TypeError, ValueError):
            max_hits = 200
        max_hits = max(1, min(max_hits, 2000))
        flags = _re.IGNORECASE if call.args.get("case_insensitive") else 0
        try:
            rx = _re.compile(pattern, flags)
        except _re.error as exc:
            return _fail(call, t0, f"bad regex: {exc}")

        hits: list[dict[str, Any]] = []
        files_scanned = 0
        try:
            for path in root.glob(glob_pat):
                if not path.is_file():
                    continue
                files_scanned += 1
                # Skip obvious binary / large files cheaply.
                try:
                    if path.stat().st_size > 5_000_000:
                        continue
                except OSError:
                    continue
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append({
                                    "path": str(path),
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:300],
                                })
                                if len(hits) >= max_hits:
                                    raise StopIteration
                except OSError:
                    continue
        except StopIteration:
            pass
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "root": str(root),
                "pattern": pattern,
                "glob": glob_pat,
                "files_scanned": files_scanned,
                "hits": hits,
                "hit_count": len(hits),
                "truncated": len(hits) >= max_hits,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _file_delete(self, call: ToolCall, t0: float) -> ToolResult:
        """B-46: cross-platform file/dir delete. Refuses non-empty dirs
        unless ``recursive=true``. Honours allowed_dirs sandbox.

        B-62: refuses to delete a sandbox root itself, even when the
        path resolves "inside" the sandbox. Otherwise an agent given
        ``allowed_dirs=["/home/proj"]`` could call
        ``file_delete("/home/proj", recursive=True)`` and nuke the
        whole project including .git — sandbox-respecting in name,
        catastrophic in effect.
        """
        import shutil as _shutil

        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing 'path'")
        path = Path(raw_path)
        try:
            path = path.resolve()
        except OSError as exc:
            return _fail(call, t0, f"bad path: {exc}")
        self._check_allowed(path)
        # B-331: workspace-containment audit. file_delete is destructive
        # so the signal is especially valuable when the agent reaches
        # outside the configured workspace.
        self._audit_workspace_containment(path, op="file_delete")
        # B-62 guard: deny deletion when path IS one of the sandbox
        # roots (not just inside them). Apply only when sandbox is on
        # — without sandbox, there's no notion of "root to protect".
        if self._allowed is not None:
            for root in self._allowed:
                try:
                    if path.samefile(root):
                        return _fail(
                            call, t0,
                            f"refused: {path} is a sandbox root; deleting "
                            f"it would wipe the entire allowlisted area",
                        )
                except OSError:
                    continue
        if not path.exists():
            return _fail(call, t0, f"path does not exist: {path}")
        recursive = bool(call.args.get("recursive", False))
        kind = "dir" if path.is_dir() else "file"
        try:
            if path.is_dir():
                if recursive:
                    _shutil.rmtree(path)
                else:
                    # rmdir refuses non-empty
                    path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            return _fail(call, t0, f"delete failed: {exc}")
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "path": str(path),
                "kind": kind,
                "recursive": recursive if kind == "dir" else False,
            },
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _cwd_default(self) -> Path:
        """Resolve the workspace root for tools that take an optional
        ``root``. Falls back to cwd when no workspace is wired."""
        try:
            if self._workspace_root_provider is not None:
                v = self._workspace_root_provider()
                if v is not None:
                    return Path(str(v))
        except Exception:  # noqa: BLE001
            pass
        return Path(".").resolve()

    # ── bash ──────────────────────────────────────────────────────────

    async def _bash(self, call: ToolCall, t0: float) -> ToolResult:
        command = call.args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _fail(call, t0, "missing or empty 'command' argument")
        cwd = call.args.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            return _fail(
                call, t0, f"'cwd' must be string, got {type(cwd).__name__}",
            )
        # Workspace fallback: when the LLM doesn't pin cwd, use the
        # active workspace root from WorkspaceManager so `pwd` / `ls`
        # land in the user's project, not wherever the daemon launched
        # from. Best-effort — provider failures fall through to None
        # which subprocess interprets as the daemon's CWD.
        if cwd is None and self._workspace_root_provider is not None:
            try:
                resolved = self._workspace_root_provider()
                if resolved is not None:
                    cwd = str(resolved)
            except Exception:  # noqa: BLE001
                cwd = None
        timeout = call.args.get("timeout_seconds", _BASH_DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = _BASH_DEFAULT_TIMEOUT

        # Shell selection. On Windows, cmd.exe doesn't understand
        # POSIX commands like ``ls``, ``cat``, ``grep``. LLMs typically
        # emit POSIX-style commands, so we route through PowerShell
        # (which has Unix-style aliases: ls, cat, pwd, rm, etc.). Fall
        # back to cmd if pwsh/powershell isn't on PATH for some reason.
        shell_exe: str | None = None
        shell_args: list[str] | None = None
        if sys.platform == "win32":
            for candidate in ("pwsh", "powershell"):
                if shutil.which(candidate):
                    shell_exe = candidate
                    shell_args = ["-NoProfile", "-Command", command]
                    break

        def _run() -> tuple[int, bytes]:
            if shell_exe is not None and shell_args is not None:
                proc = subprocess.run(
                    [shell_exe, *shell_args],
                    shell=False, cwd=cwd,
                    capture_output=True, timeout=timeout,
                )
            else:
                proc = subprocess.run(
                    command, shell=True, cwd=cwd,
                    capture_output=True, timeout=timeout,
                )
            merged = (proc.stdout or b"") + (proc.stderr or b"")
            return proc.returncode, merged

        try:
            code, merged = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return _fail(call, t0, f"timed out after {timeout}s")
        text = merged.decode("utf-8", errors="replace")
        if len(text) > _BASH_MAX_OUTPUT:
            text = text[:_BASH_MAX_OUTPUT] + f"\n...[truncated, {len(merged)} bytes total]"
        content = f"[exit {code}]\n{text}"
        return ToolResult(
            call_id=call.id,
            ok=(code == 0),
            content=content,
            error=None if code == 0 else f"command exited non-zero ({code})",
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── web tools ─────────────────────────────────────────────────────

    async def _web_fetch(self, call: ToolCall, t0: float) -> ToolResult:
        url = call.args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _fail(call, t0, "missing or empty 'url' argument")
        if not (url.startswith("http://") or url.startswith("https://")):
            return _fail(call, t0, f"url must start with http(s)://, got {url!r}")
        max_chars = call.args.get("max_chars", _MAX_WEB_BYTES)
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = _MAX_WEB_BYTES

        import httpx
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as c:
                r = await c.get(url, headers={
                    "User-Agent": "XMclaw/2.x (+local)",
                })
        except httpx.HTTPError as exc:
            # B-233: ``str(exc)`` is EMPTY for several httpx exception
            # types (ConnectError without a wrapped OSError, ProtocolError,
            # certain TLS handshake aborts). Pre-B-233 the agent saw
            # ``http error: `` with nothing after the colon and kept
            # retrying the same URL, eating context — real-data
            # (chat-18e1711d) had 5+ identical empty-error retries
            # adding up to a 262K-token request. Always include the
            # exception class name; fall back to ``repr(exc)`` when
            # ``str()`` returns empty so SOMETHING surfaces.
            err_msg = str(exc) or repr(exc)
            return _fail(
                call, t0,
                f"http error: {type(exc).__name__}: {err_msg}",
            )
        text = r.text
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        suffix = f"\n...[truncated to {max_chars} chars]" if truncated else ""
        content = (
            f"[{r.status_code} {r.reason_phrase}] {url}\n"
            f"{text}{suffix}"
        )
        return ToolResult(
            call_id=call.id,
            ok=(200 <= r.status_code < 400),
            content=content,
            error=None if 200 <= r.status_code < 400 else f"HTTP {r.status_code}",
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _web_search(self, call: ToolCall, t0: float) -> ToolResult:
        query = call.args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _fail(call, t0, "missing or empty 'query' argument")
        max_results = call.args.get("max_results", 5)
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 20))

        import httpx
        # DuckDuckGo's "html" endpoint is the most reliable no-key search.
        url = "https://duckduckgo.com/html/"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as c:
                r = await c.post(
                    url, data={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 XMclaw/2.x"},
                )
        except httpx.HTTPError as exc:
            # B-233: same empty-str(exc) trap as web_fetch.
            err_msg = str(exc) or repr(exc)
            return _fail(
                call, t0,
                f"search error: {type(exc).__name__}: {err_msg}",
            )
        if r.status_code != 200:
            return _fail(call, t0, f"search returned HTTP {r.status_code}")
        results = _parse_ddg_html(r.text, max_results)
        if not results:
            return ToolResult(
                call_id=call.id, ok=True,
                content=f"(no results for {query!r})",
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        blocks = [
            f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ]
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"{len(results)} results for {query!r}:\n\n" + "\n\n".join(blocks),
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── todos (per-session plan tracker) ───────────────────────────────

    def _todo_key(self, call: ToolCall) -> str:
        # ToolCall.session_id is populated by AgentLoop. Anonymous callers
        # (e.g. direct unit tests) share the "_default" bucket.
        return call.session_id or "_default"

    async def _todo_write(self, call: ToolCall, t0: float) -> ToolResult:
        items = call.args.get("items")
        if not isinstance(items, list):
            return _fail(call, t0, "'items' must be a list")
        cleaned: list[dict[str, str]] = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                return _fail(
                    call, t0,
                    f"item {i} must be an object with content + status",
                )
            content = raw.get("content")
            status = raw.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                return _fail(call, t0, f"item {i}: content must be non-empty string")
            if status not in _VALID_TODO_STATUSES:
                return _fail(
                    call, t0,
                    f"item {i}: status {status!r} must be one of "
                    f"{sorted(_VALID_TODO_STATUSES)}",
                )
            cleaned.append({"content": content.strip(), "status": status})

        sid = self._todo_key(call)
        self._todos[sid] = cleaned
        if self._todo_listener is not None:
            try:
                self._todo_listener(sid, list(cleaned))
            except Exception:  # noqa: BLE001 -- listener must never sink a tool call
                pass

        done = sum(1 for t in cleaned if t["status"] == "done")
        prog = sum(1 for t in cleaned if t["status"] == "in_progress")
        summary = f"saved {len(cleaned)} todos ({done} done, {prog} in progress)"
        return ToolResult(
            call_id=call.id, ok=True,
            content=summary,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _todo_read(self, call: ToolCall, t0: float) -> ToolResult:
        sid = self._todo_key(call)
        items = self._todos.get(sid, [])
        if not items:
            body = "(no todos yet)"
        else:
            def _glyph(s: str) -> str:
                return {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}.get(s, "[?]")
            body = "\n".join(
                f"{i+1}. {_glyph(t['status'])} {t['content']}"
                for i, t in enumerate(items)
            )
        return ToolResult(
            call_id=call.id, ok=True, content=body,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── self-modifying memory tools ───────────────────────────────────

    async def _remember(self, call: ToolCall, t0: float) -> ToolResult:
        category = call.args.get("category")
        note = call.args.get("note")
        if not isinstance(category, str) or not category.strip():
            return _fail(call, t0, "missing or empty 'category'")
        if not isinstance(note, str) or not note.strip():
            return _fail(call, t0, "missing or empty 'note'")
        return await self._append_persona(
            call, t0,
            basename="MEMORY.md",
            section=category.strip(),
            entry=note.strip(),
            placeholder_title="MEMORY.md — what I want to remember next time",
        )

    async def _memory_pin(self, call: ToolCall, t0: float) -> ToolResult:
        """B-53: pin a fact to MEMORY.md's ``## Pinned`` section. Same
        write path as ``remember``, just under a section the dream
        prompt is told to preserve verbatim."""
        content = call.args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _fail(call, t0, "missing or empty 'content'")
        return await self._append_persona(
            call, t0,
            basename="MEMORY.md",
            section="Pinned",
            entry=content.strip(),
            placeholder_title="MEMORY.md — what I want to remember next time",
        )

    async def _learn_about_user(self, call: ToolCall, t0: float) -> ToolResult:
        section = call.args.get("section")
        fact = call.args.get("fact")
        if not isinstance(section, str) or not section.strip():
            return _fail(call, t0, "missing or empty 'section'")
        if not isinstance(fact, str) or not fact.strip():
            return _fail(call, t0, "missing or empty 'fact'")
        return await self._append_persona(
            call, t0,
            basename="USER.md",
            section=section.strip(),
            entry=fact.strip(),
            placeholder_title="USER.md — who I'm working with",
        )

    async def _schedule_followup(self, call: ToolCall, t0: float) -> ToolResult:
        """Create a cron job — agent's self-scheduling primitive.

        Wraps :class:`xmclaw.core.scheduler.cron.CronStore` so the agent
        can set its own reminders without learning the full
        ``/api/v2/cron`` REST surface. ``run_once=True`` is implemented
        by appending a deletion clause to the prompt — the future agent
        deletes its own job after firing.
        """
        name = call.args.get("name")
        schedule = call.args.get("schedule")
        prompt = call.args.get("prompt")
        run_once = bool(call.args.get("run_once", False))
        if not isinstance(name, str) or not name.strip():
            return _fail(call, t0, "missing or empty 'name'")
        if not isinstance(schedule, str) or not schedule.strip():
            return _fail(call, t0, "missing or empty 'schedule'")
        if not isinstance(prompt, str) or not prompt.strip():
            return _fail(call, t0, "missing or empty 'prompt'")

        # B-37: run_once is now a real CronJob field — CronStore.mark_fired
        # deletes the job after firing instead of rescheduling. No more
        # "future agent please delete yourself" breadcrumbs.
        full_prompt = prompt.strip()

        try:
            from xmclaw.core.scheduler.cron import CronJob, default_cron_store
            store = default_cron_store()
            import uuid as _uuid
            job = CronJob(
                id=_uuid.uuid4().hex,
                name=name.strip(),
                schedule=schedule.strip(),
                prompt=full_prompt,
                run_once=run_once,
            )
            saved = store.add(job)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"schedule failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "job_id": saved.id,
                "name": saved.name,
                "schedule": saved.schedule,
                "next_run_at": saved.next_run_at,
                "run_once": run_once,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _note_write(self, call: ToolCall, t0: float) -> ToolResult:
        """B-45: agent-driven write to ~/.xmclaw/memory/*.md.

        Lands in the Web UI's Notes tab + gets vector-indexed by
        the next indexer tick. Used by the agent to record workflows,
        lessons learned, accumulated reference — first-class evolution
        surface alongside MEMORY.md.
        """
        from xmclaw.utils.paths import file_memory_dir

        name = str(call.args.get("name") or "").strip()
        content = call.args.get("content")
        mode = str(call.args.get("mode") or "replace").lower()
        description = str(call.args.get("description") or "").strip()
        tags_raw = call.args.get("tags")
        tags: list[str] = []
        if isinstance(tags_raw, list):
            tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        if not name:
            return _fail(call, t0, "missing 'name'")
        if not isinstance(content, str):
            return _fail(call, t0, "'content' must be a string")
        if mode not in ("replace", "append"):
            return _fail(call, t0, f"unknown mode {mode!r}")

        # Strip path components for safety, ensure .md.
        safe = name.replace("\\", "/").split("/")[-1].strip()
        if not safe or safe.startswith("."):
            return _fail(call, t0, f"invalid note name {name!r}")
        if not safe.endswith(".md"):
            safe = safe + ".md"

        mdir = file_memory_dir()
        try:
            mdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"mkdir failed: {exc}")
        path = mdir / safe

        # B-93: build YAML-style frontmatter when description/tags
        # passed. Only on replace mode — append preserves whatever
        # frontmatter the file already had.
        def _build_frontmatter() -> str:
            if not description and not tags:
                return ""
            lines = ["---"]
            if description:
                # Escape any literal \"---\" inside the description
                # so it can't terminate the block early.
                clean = description.replace("---", "—")
                lines.append(f"description: {clean}")
            if tags:
                lines.append("tags: [" + ", ".join(tags) + "]")
            lines.append("---")
            lines.append("")  # blank line before body
            return "\n".join(lines) + "\n"

        # B-64: lock the file so concurrent note_write calls (or note +
        # daemon-side editor write via /api/v2/memory POST) don't race
        # on the read-modify-write append path.
        from xmclaw.utils.fs_locks import atomic_write_text
        async with self._fs_lock(path):
            try:
                if mode == "append" and path.is_file():
                    existing = path.read_text(encoding="utf-8", errors="replace")
                    sep = "\n\n---\n\n" if existing.strip() else ""
                    atomic_write_text(
                        path,
                        existing.rstrip() + sep + content.strip() + "\n",
                    )
                else:
                    body = _build_frontmatter() + content
                    atomic_write_text(path, body)
            except OSError as exc:
                return _fail(call, t0, f"write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "name": safe,
                "path": str(path),
                "mode": mode,
                "size": path.stat().st_size,
            },
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _journal_append(self, call: ToolCall, t0: float) -> ToolResult:
        """B-45: append a dated entry to ~/.xmclaw/memory/journal/<date>.md.

        Web UI's Journal tab reads from the same path. Each entry gets
        a horizontal rule separator + an HH:MM:SS timestamp. Optional
        ``title`` becomes a ## heading for table-of-contents-style
        scanning later.
        """
        from xmclaw.utils.paths import file_memory_dir
        import re as _re

        content = call.args.get("content")
        date = str(call.args.get("date") or "").strip() or time.strftime("%Y-%m-%d")
        title = str(call.args.get("title") or "").strip()

        if not isinstance(content, str) or not content.strip():
            return _fail(call, t0, "missing 'content'")
        # Reject malformed dates rather than silently writing to a
        # weird filename — agent sometimes hands us "today" as the
        # literal string.
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return _fail(
                call, t0,
                f"date must be YYYY-MM-DD (got {date!r})",
            )

        jdir = file_memory_dir() / "journal"
        try:
            jdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"mkdir failed: {exc}")

        path = jdir / f"{date}.md"
        ts = time.strftime("%H:%M:%S")
        block_parts: list[str] = []
        if title:
            block_parts.append(f"## {title}")
        block_parts.append(f"_{ts}_")
        block_parts.append(content.strip())
        block = "\n\n".join(block_parts)

        # B-64: same RMW lock as note_write — concurrent agent +
        # cron append on the same daily file would otherwise lose
        # entries.
        from xmclaw.utils.fs_locks import atomic_write_text
        async with self._fs_lock(path):
            try:
                if path.is_file():
                    existing = path.read_text(encoding="utf-8", errors="replace")
                    if not existing.startswith("# "):
                        existing = f"# 日记 {date}\n\n" + existing
                    atomic_write_text(
                        path,
                        existing.rstrip() + "\n\n---\n\n" + block + "\n",
                    )
                else:
                    atomic_write_text(
                        path,
                        f"# 日记 {date}\n\n" + block + "\n",
                    )
            except OSError as exc:
                return _fail(call, t0, f"write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "date": date,
                "path": str(path),
                "size": path.stat().st_size,
                "title": title or None,
            },
            side_effects=(str(path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _recall_user_preferences(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """Epic #24 Phase 4.2: read USER.md auto-extracted preferences.

        Parses the ``## Auto-extracted preferences`` section written
        by ProfileExtractor. Each line follows the
        ``ProfileDelta.render_line()`` shape::

            - [auto · {kind} · conf={confidence:.2f} · session={sid}] {text}

        Optional ``topic`` substring filter (case-insensitive) +
        ``kind`` exact filter + ``limit`` cap. Returns [] cleanly
        when no auto-extracted entries exist yet.
        """
        import re as _re_pref

        topic = (call.args.get("topic") or "").strip().lower()
        kind = (call.args.get("kind") or "").strip().lower()
        limit_raw = call.args.get("limit", 10)
        try:
            limit = max(1, min(50, int(limit_raw)))
        except (TypeError, ValueError):
            return _fail(call, t0, f"limit must be integer (got {limit_raw!r})")

        if self._persona_dir_provider is None:
            return _fail(
                call, t0,
                "recall_user_preferences not configured (no persona dir)",
            )
        try:
            persona_root = Path(self._persona_dir_provider())
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"persona dir resolution failed: {exc}")

        user_md = persona_root / "USER.md"
        if not user_md.is_file():
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "USER.md not yet created — no extracted preferences",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        try:
            text = user_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _fail(call, t0, f"USER.md read failed: {exc}")

        # Locate the section. ProfileExtractor writes / appends below
        # the heading "## Auto-extracted preferences".
        heading = "## Auto-extracted preferences"
        idx = text.find(heading)
        if idx < 0:
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "USER.md has no `## Auto-extracted "
                            "preferences` section yet — ProfileExtractor "
                            "hasn't flushed any deltas",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        section = text[idx + len(heading):]
        # Stop at the next top-level heading so we don't bleed into
        # whatever the user / hand-curated content put after.
        nxt = section.find("\n## ")
        if nxt > 0:
            section = section[:nxt]

        # Match lines emitted by ProfileDelta.render_line(). Tolerant
        # of whitespace + accepts both ASCII and CJK middle dots
        # (·) so future renderer tweaks don't silently break the
        # parser.
        pattern = _re_pref.compile(
            r"^\s*-\s*\[auto\s*[·.]\s*([^·.\]]+?)\s*[·.]\s*conf=([\d.]+)\s*"
            r"[·.]\s*session=([^\]]+?)\]\s*(.+)\s*$"
        )
        entries: list[dict[str, Any]] = []
        for line in section.splitlines():
            m = pattern.match(line)
            if m is None:
                continue
            entry_kind = m.group(1).strip().lower()
            try:
                conf = float(m.group(2))
            except ValueError:
                continue
            entry_session = m.group(3).strip()
            entry_text = m.group(4).strip()
            if kind and entry_kind != kind:
                continue
            if topic and topic not in entry_text.lower():
                continue
            entries.append({
                "kind": entry_kind,
                "text": entry_text,
                "confidence": round(conf, 3),
                "session": entry_session,
            })
            if len(entries) >= limit:
                break

        return ToolResult(
            call_id=call.id, ok=True,
            content={"entries": entries, "matched": len(entries)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _journal_recall(self, call: ToolCall, t0: float) -> ToolResult:
        """Epic #24 Phase 2.5: read past session journals.

        Loads ``JournalReader`` lazily so a config without persona/
        memory wiring still surfaces the tool cleanly. Filters:

        * ``limit`` (1-50, default 5)
        * ``days_back`` (default 30) drops entries older than that
        * ``contains`` substring filter on tool name list

        Returns one dict per matching entry with the journal fields
        the agent typically wants to reason about (session_id,
        ts_end ISO, duration_s, turn_count, tool names, grader avg).
        """
        from xmclaw.core.journal import JournalReader

        limit_raw = call.args.get("limit", 5)
        days_back_raw = call.args.get("days_back", 30)
        contains = (call.args.get("contains") or "").strip().lower()

        try:
            limit = max(1, min(50, int(limit_raw)))
        except (TypeError, ValueError):
            return _fail(call, t0, f"limit must be integer (got {limit_raw!r})")
        try:
            days_back = max(1, int(days_back_raw))
        except (TypeError, ValueError):
            return _fail(
                call, t0,
                f"days_back must be integer (got {days_back_raw!r})",
            )

        reader = JournalReader()
        # Pull a couple extra so the days_back / contains filters have
        # room to drop without ending up under the requested limit.
        candidates = reader.recent(limit=max(limit * 4, 20))
        if not candidates:
            return ToolResult(
                call_id=call.id, ok=True,
                content={
                    "entries": [],
                    "note": "journal directory empty — no prior sessions yet",
                },
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        cutoff = time.time() - days_back * 86400
        out: list[dict] = []
        for entry in candidates:
            if entry.ts_end < cutoff:
                continue
            tool_names = [tc.name for tc in entry.tool_calls]
            if contains and not any(
                contains in (n or "").lower() for n in tool_names
            ):
                continue
            out.append({
                "session_id": entry.session_id,
                "ts_end_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(entry.ts_end),
                ),
                "duration_s": round(entry.duration_s, 1),
                "turn_count": entry.turn_count,
                "tool_names": tool_names,
                "tool_errors": sum(
                    1 for tc in entry.tool_calls if not tc.ok
                ),
                "grader_avg": (
                    round(entry.grader_avg_score, 3)
                    if entry.grader_avg_score is not None else None
                ),
                "grader_play_count": entry.grader_play_count,
                "anti_req_violations": entry.anti_req_violations,
            })
            if len(out) >= limit:
                break

        return ToolResult(
            call_id=call.id, ok=True,
            content={"entries": out, "matched": len(out)},
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_compact(self, call: ToolCall, t0: float) -> ToolResult:
        """B-52: trigger Auto-Dream now (instead of waiting for the
        daily cron). Reaches the running compactor via the same
        ``_LAST_APP_STATE`` holder factory.py uses for persona-writeback.
        Refuses cleanly when no LLM is configured."""
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
        except Exception:  # noqa: BLE001
            state = None
        if state is None:
            return _fail(call, t0, "daemon not started (no app.state available)")
        compactor = getattr(state, "dream_compactor", None)
        if compactor is None:
            return _fail(
                call, t0,
                "memory_compact unavailable: no LLM configured for dream",
            )
        result = await compactor.dream()
        return ToolResult(
            call_id=call.id, ok=bool(result.get("ok")),
            content=result,
            error=None if result.get("ok") else result.get("error"),
            side_effects=(result.get("memory_path") or "",) if result.get("ok") else (),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _enter_worktree(self, call: ToolCall, t0: float) -> ToolResult:
        """B-94 + B-235: create ``.xmworktrees/<name>/`` + new branch and
        switch the daemon's primary workspace into it.

        Refuses when:
          * not inside a git repo (``git rev-parse`` fails)
          * already inside a worktree (path under .xmworktrees/ OR the
            legacy .claude/worktrees/ — both checked for back-compat)
        Both check messages tell the agent what to do next.

        B-235 path migration: pre-B-235 worktrees lived under
        ``.claude/worktrees/<name>/`` — Claude Code's project-level
        namespace. ``enter_worktree`` now writes to ``.xmworktrees/``
        instead so XMclaw stays out of other agents' territory.
        ``exit_worktree`` accepts both paths for back-compat — users
        with in-flight ``.claude/worktrees/`` worktrees can still wind
        them down without the daemon refusing.
        """
        from xmclaw.core.workspace import WorkspaceManager

        # 1. Resolve current primary root.
        wm = WorkspaceManager()
        state = wm.get()
        if state.primary is None:
            return _fail(
                call, t0,
                "no primary workspace — register one with the "
                "WorkspaceManager first (or call from a daemon that "
                "auto-loaded a project root)",
            )
        original_root = state.primary.path
        # Reject if already in a worktree — nesting just creates
        # confusion and the cleanup path can't tell what to undo.
        # B-235: detect both new (.xmworktrees) AND legacy
        # (.claude/worktrees) layouts so the "already in a worktree"
        # guard still fires for users still inside a pre-B-235 worktree.
        _root_parts = original_root.parts
        _in_xm_worktree = (
            "xmworktrees" in _root_parts
            and any(
                p == ".xmworktrees" for p in _root_parts
            )
        )
        _in_legacy_worktree = (
            ".claude" in _root_parts and "worktrees" in _root_parts
        )
        if _in_xm_worktree or _in_legacy_worktree:
            return _fail(
                call, t0,
                "already inside a worktree — call ``exit_worktree`` "
                "first if you want to swap into a new one",
            )

        # 2. Confirm the original root is a git repo.
        try:
            check = subprocess.run(
                ["git", "-C", str(original_root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return _fail(call, t0, f"git unavailable: {exc}")
        if check.returncode != 0 or check.stdout.strip() != "true":
            return _fail(
                call, t0,
                f"{original_root} is not a git repository — "
                "worktrees only work inside git repos",
            )

        # 3. Pick worktree name + branch. Strip slashes etc — git rejects
        # weird tokens but a clean name keeps the dir layout neat.
        raw_name = str(call.args.get("name") or "").strip()
        raw_name = re.sub(r"[^a-zA-Z0-9._-]", "-", raw_name).strip("-._")
        if not raw_name:
            # Random adjective-noun: stable enough for humans to type
            # without relying on a wordlist file.
            import random as _rnd
            adjectives = ("quick", "calm", "spicy", "bold", "nimble", "still")
            nouns = ("otter", "panda", "ember", "river", "forge", "pebble")
            raw_name = (
                f"{_rnd.choice(adjectives)}-{_rnd.choice(nouns)}-"
                f"{uuid.uuid4().hex[:6]}"
            )
        # B-235: write to <repo>/.xmworktrees/<name>/ instead of
        # <repo>/.claude/worktrees/<name>/.
        wt_path = original_root / ".xmworktrees" / raw_name
        if wt_path.exists():
            return _fail(
                call, t0,
                f"worktree path already exists: {wt_path} — "
                "pick a different name or remove the leftover dir",
            )
        # Branch name: prefix to avoid colliding with regular branches
        # the user creates manually.
        branch = f"wt/{raw_name}"
        base_branch = str(call.args.get("base_branch") or "").strip()

        # 4. ``git worktree add -b <branch> <path> [<base>]``.
        cmd = [
            "git", "-C", str(original_root),
            "worktree", "add", "-b", branch, str(wt_path),
        ]
        if base_branch:
            cmd.append(base_branch)
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            return _fail(call, t0, f"git worktree add timed out: {exc}")
        if res.returncode != 0:
            return _fail(
                call, t0,
                f"git worktree add failed: {(res.stderr or res.stdout).strip()}",
            )

        # 5. Register the new worktree as primary; remember the origin
        # so ``exit_worktree`` can walk back.
        wm.add(wt_path, name=raw_name)
        _WORKTREE_ORIGIN[str(wt_path.resolve())] = original_root

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "worktree_path": str(wt_path),
                "branch": branch,
                "original_root": str(original_root),
                "base_branch": base_branch or "HEAD",
            },
            side_effects=(str(wt_path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _exit_worktree(self, call: ToolCall, t0: float) -> ToolResult:
        """B-94: leave the current worktree, optionally remove it.

        Validates that the current primary actually IS a worktree
        before doing anything destructive — refuses to run otherwise.
        """
        from xmclaw.core.workspace import WorkspaceManager

        keep = bool(call.args.get("keep", False))

        wm = WorkspaceManager()
        state = wm.get()
        if state.primary is None:
            return _fail(call, t0, "no primary workspace registered")
        wt_path = state.primary.path
        # B-235: worktree directory must live under .xmworktrees/ (new
        # default) OR .claude/worktrees/ (legacy, back-compat). The
        # check is the cheap heuristic that prevents an accidental
        # ``exit_worktree`` from wiping the user's main checkout.
        wt_str = str(wt_path).replace("\\", "/") + "/"
        _under_xm = "/.xmworktrees/" in wt_str
        _under_legacy = "/.claude/worktrees/" in wt_str
        if not (_under_xm or _under_legacy):
            return _fail(
                call, t0,
                "current primary is not a worktree under "
                ".xmworktrees/ (or legacy .claude/worktrees/) — "
                "refusing to act",
            )

        # Look up the origin we recorded on enter. Fall back to git's
        # own ``worktree list`` if we lost track (daemon restart, etc).
        origin = _WORKTREE_ORIGIN.get(str(wt_path.resolve()))
        if origin is None:
            try:
                res = subprocess.run(
                    ["git", "-C", str(wt_path), "rev-parse", "--show-superproject-working-tree"],
                    capture_output=True, text=True, timeout=10,
                )
                if res.returncode == 0 and res.stdout.strip():
                    origin = Path(res.stdout.strip())
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            if origin is None:
                # B-235: walk up to recover origin repo.
                # New layout: <repo>/.xmworktrees/<name> → parents
                #   [0]=.xmworktrees, [1]=<repo>; origin = parents[1]
                # Legacy:    <repo>/.claude/worktrees/<name> → parents
                #   [0]=worktrees, [1]=.claude, [2]=<repo>; origin = parents[2]
                _parts = wt_path.parts
                if len(_parts) >= 3 and _parts[-2] == ".xmworktrees":
                    origin = wt_path.parents[1]
                elif len(_parts) >= 4 and _parts[-3:-1] == (
                    ".claude", "worktrees",
                ):
                    origin = wt_path.parents[2]
                elif wt_path.parents[1].name == "worktrees":
                    # Defensive fallback for unusual layouts.
                    origin = wt_path.parents[2]
        if origin is None or not origin.exists():
            return _fail(
                call, t0,
                "couldn't determine origin repo for this worktree — "
                "the agent may need to manually `cd` to the parent",
            )

        # Read the current branch name so we can drop it after removal.
        branch_name: str | None = None
        try:
            br = subprocess.run(
                ["git", "-C", str(wt_path), "branch", "--show-current"],
                capture_output=True, text=True, timeout=10,
            )
            if br.returncode == 0:
                branch_name = (br.stdout or "").strip() or None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Switch primary back to the origin BEFORE removing the worktree
        # dir — otherwise WorkspaceManager could end up with a dangling
        # primary entry pointing at a vanished path.
        wm.add(origin)  # add() returns existing entry when already present + makes it primary
        wm.remove(wt_path)
        _WORKTREE_ORIGIN.pop(str(wt_path.resolve()), None)

        removed = False
        if not keep:
            try:
                rm = subprocess.run(
                    ["git", "-C", str(origin), "worktree", "remove", "--force", str(wt_path)],
                    capture_output=True, text=True, timeout=30,
                )
                removed = rm.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                removed = False
            # Drop the branch too — it has no commits worth keeping in
            # the default-discard path. Best-effort; keep returning OK
            # even if the branch delete fails (the worktree is gone,
            # which is the user-visible cleanup goal).
            if removed and branch_name:
                try:
                    subprocess.run(
                        ["git", "-C", str(origin), "branch", "-D", branch_name],
                        capture_output=True, text=True, timeout=10,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "returned_to": str(origin),
                "worktree_path": str(wt_path),
                "branch": branch_name,
                "kept": keep,
                "removed": removed,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _ask_user_question(self, call: ToolCall, t0: float) -> ToolResult:
        """B-92: stop the turn, publish AGENT_ASKED_QUESTION, block on a
        Future until the WS handler resolves it with the user's answer.

        Cross-boundary plumbing: the future lives in the module-level
        :data:`_PENDING_QUESTIONS` dict so both this tool (which awaits
        it) and ``daemon/app.py`` 's WS handler (which resolves it on
        the answer_question client frame) share the same identity.

        Timeout caps the wait at 600 seconds — past that we return
        ``ok=False`` so the agent can recover and proceed with its
        best guess instead of hanging indefinitely.
        """
        question = str(call.args.get("question") or "").strip()
        options = call.args.get("options") or []
        multi = bool(call.args.get("multi_select"))
        allow_other = bool(call.args.get("allow_other", True))
        if not question:
            return _fail(call, t0, "missing 'question'")
        if not isinstance(options, list) or not options:
            return _fail(call, t0, "options must be a non-empty list")
        # Normalise options to {label, value, description?} dicts.
        norm_options: list[dict[str, str]] = []
        for i, o in enumerate(options):
            if not isinstance(o, dict):
                return _fail(call, t0, f"options[{i}] must be an object")
            label = str(o.get("label") or "").strip()
            value = str(o.get("value") or "").strip()
            if not label or not value:
                return _fail(call, t0, f"options[{i}] needs both 'label' and 'value'")
            entry = {"label": label, "value": value}
            desc = o.get("description")
            if isinstance(desc, str) and desc.strip():
                entry["description"] = desc.strip()
            norm_options.append(entry)

        question_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        _PENDING_QUESTIONS[question_id] = future
        # B-99: payload snapshot for the reconnect-recovery endpoint.
        # Front-end calls ``GET /api/v2/pending_questions`` on WS open
        # so a browser refresh while an ask is in flight rebuilds the
        # card instead of stranding the future.
        _PENDING_QUESTION_PAYLOADS[question_id] = {
            "question_id": question_id,
            "question": question,
            "options": norm_options,
            "multi_select": multi,
            "allow_other": allow_other,
            "tool_call_id": call.id,
        }

        # Publish AGENT_ASKED_QUESTION via the bus the daemon factory
        # supplies. Same indirection pattern persona-writeback uses
        # (_LAST_APP_STATE) so this tool stays decoupled from the
        # daemon module.
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
            bus = getattr(state, "bus", None) if state is not None else None
        except Exception:  # noqa: BLE001
            bus = None
        if bus is not None:
            try:
                from xmclaw.core.bus import EventType, make_event
                # B-237: use the REAL session_id (set by AgentLoop on
                # the ToolCall before invoke). Pre-B-237 this was
                # hardcoded to ``"_question"`` — a placeholder that
                # never matches the front-end's WS session
                # subscription, so the event silently dropped on the
                # gateway floor. The QuestionCard only became visible
                # after page refresh because the rehydrate path
                # (``GET /api/v2/pending_questions``) is HTTP and
                # session-agnostic. Live path was broken since B-92.
                # Fall back to ``"_question"`` only for defensive
                # callers that build a ToolCall without a session_id.
                sid = call.session_id or "_question"
                ev = make_event(
                    session_id=sid,
                    agent_id="main",
                    type=EventType.AGENT_ASKED_QUESTION,
                    payload={
                        "question_id": question_id,
                        "question": question,
                        "options": norm_options,
                        "multi_select": multi,
                        "allow_other": allow_other,
                        "tool_call_id": call.id,
                    },
                )
                await bus.publish(ev)
            except Exception:  # noqa: BLE001 — telemetry path; never block
                pass

        try:
            answer = await asyncio.wait_for(future, timeout=600.0)
        except asyncio.TimeoutError:
            return _fail(
                call, t0,
                "user did not respond within 10 minutes — proceed with "
                "your best guess or ask again differently",
            )
        finally:
            _PENDING_QUESTIONS.pop(question_id, None)
            _PENDING_QUESTION_PAYLOADS.pop(question_id, None)

        # ``answer`` is a string for single-select, list for multi-select,
        # or a free-text "Other" string. Caller (the LLM) sees it as
        # plain text in the tool result.
        if isinstance(answer, list):
            return ToolResult(
                call_id=call.id, ok=True,
                content=", ".join(str(a) for a in answer),
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return ToolResult(
            call_id=call.id, ok=True,
            content=str(answer),
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _agent_status(self, call: ToolCall, t0: float) -> ToolResult:
        """B-49: self-introspection. Reads daemon state via the same
        ``_LAST_APP_STATE`` holder factory.py uses for persona-writeback,
        so works without forcing every BuiltinTools instance to carry
        an explicit app.state reference."""
        out: dict[str, Any] = {}
        # 1) Memory layer — providers + indexer.
        if self._memory_manager is not None:
            providers = []
            for p in getattr(self._memory_manager, "providers", []):
                providers.append({
                    "name": getattr(p, "name", "?"),
                    "kind": "builtin" if getattr(p, "name", "") == "builtin" else "external",
                })
            out["memory"] = {
                "wired": True,
                "providers": providers,
                "embedder": (
                    {"name": getattr(self._embedder, "name", "?"),
                     "dim": getattr(self._embedder, "dim", 0)}
                    if self._embedder is not None else None
                ),
            }
        else:
            out["memory"] = {"wired": False}

        # 2) Daemon-side state via _LAST_APP_STATE.
        state = None
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
        except Exception:  # noqa: BLE001
            state = None

        if state is not None:
            # Indexer
            idx = getattr(state, "memory_indexer", None)
            if idx is not None:
                out["indexer"] = {
                    "wired": True,
                    "running": getattr(idx, "is_running", False),
                    "watched_paths_count": sum(1 for _ in getattr(idx, "_watched_paths", lambda: [])()),
                    "known_paths_count": len(getattr(idx, "_known_paths", set()) or set()),
                    "poll_interval_s": getattr(idx, "_poll_s", None),
                }
            else:
                out["indexer"] = {"wired": False}

            # Epic #24 Phase 1: removed auto_evo subsystem status —
            # `app.state.auto_evo_process` no longer exists. Phase 2
            # will surface the EvolutionAgent observer's running state
            # here through `app.state.evolution_observer` instead.

            # Bus event count proxy via the events DB row count when
            # the daemon's running. Cheap query.
            try:
                import sqlite3 as _sql
                from xmclaw.utils.paths import data_dir
                events_db = data_dir() / "v2" / "events.db"
                if events_db.is_file():
                    con = _sql.connect(f"file:{events_db}?mode=ro", uri=True, timeout=2)
                    try:
                        n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                        out["events_db"] = {"row_count": int(n)}
                    finally:
                        con.close()
            except Exception:  # noqa: BLE001
                out["events_db"] = {"row_count": None}

        # 3) Cron — singleton, always reachable.
        try:
            from xmclaw.core.scheduler.cron import default_cron_store
            store = default_cron_store()
            jobs = store.list_jobs()
            next_at = min((j.next_run_at for j in jobs if j.enabled and j.next_run_at), default=None)
            out["cron"] = {
                "job_count": len(jobs),
                "enabled_count": sum(1 for j in jobs if j.enabled),
                "next_run_at": next_at,
            }
        except Exception:  # noqa: BLE001
            out["cron"] = {"wired": False}

        # 4) Workspace + persona dirs (resolved lazily).
        try:
            if self._workspace_root_provider is not None:
                v = self._workspace_root_provider()
                out["workspace_root"] = str(v) if v else None
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._persona_dir_provider is not None:
                v = self._persona_dir_provider()
                out["persona_dir"] = str(v) if v else None
        except Exception:  # noqa: BLE001
            pass

        return ToolResult(
            call_id=call.id, ok=True,
            content=out,
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _memory_search(self, call: ToolCall, t0: float) -> ToolResult:
        """B-40: unified memory_search — fan a query across every wired
        memory provider via MemoryManager.query.

        B-42: when an EmbeddingProvider is wired, the query gets
        embedded first and the manager routes the dense vector to
        SqliteVecMemory's KNN path — real semantic hits, not just
        substring. Without an embedder we fall through to the
        keyword path (same behaviour as B-40).

        Returns up to k hits per provider, each row carrying its
        originating provider in metadata.provider so the agent can
        tell vector hits from persona-bullet keyword hits.
        """
        query = str(call.args.get("query") or "").strip()
        if not query:
            return _fail(call, t0, "missing 'query'")
        try:
            k = int(call.args.get("k") or 5)
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 20))
        layer = str(call.args.get("layer") or "long")
        if layer not in ("short", "working", "long"):
            return _fail(call, t0, f"unknown layer: {layer!r}")

        # B-197: optional kind filter — agent narrows to one record
        # type (preference / lesson / principle / etc.) instead of
        # searching across the whole store. Implemented as a metadata
        # filter forwarded to MemoryProvider.query — sqlite_vec already
        # supports `filters={"kind": ...}` on both vector and keyword
        # paths via _filter_sql.
        kind_filter = (call.args.get("kind") or "").strip() or None
        filters: dict[str, Any] | None = (
            {"kind": kind_filter} if kind_filter else None
        )

        # B-42: try semantic via the embedder; fall back to keyword on
        # any failure so an embedding outage degrades gracefully.
        embedding: list[float] | None = None
        used_mode = "keyword"
        if self._embedder is not None:
            try:
                vecs = await self._embedder.embed([query])
                if vecs and vecs[0]:
                    embedding = list(vecs[0])
                    used_mode = "semantic"
            except Exception:  # noqa: BLE001
                embedding = None

        try:
            # B-50: hybrid Vector + keyword RRF when both signals are
            # available; manager falls back to plain vector / keyword
            # for providers that don't implement hybrid_query.
            hits = await self._memory_manager.query(  # type: ignore[union-attr]
                layer, text=query, embedding=embedding, k=k, hybrid=True,
                filters=filters,
            )
        except TypeError:
            # Older MemoryManager without hybrid kwarg — still works.
            hits = await self._memory_manager.query(  # type: ignore[union-attr]
                layer, text=query, embedding=embedding, k=k, filters=filters,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"memory_search failed: {exc}")
        # Tag the mode reflecting what the manager actually used.
        if embedding and used_mode == "semantic":
            used_mode = "hybrid"

        # B-53: total-chars cap so a wide search doesn't flood the
        # context. Defaults to 6000 chars (~1500 tokens at chars/4) —
        # enough for ~15 chunks of typical MEMORY.md size, well under
        # most context windows. The agent can opt for a shorter cap
        # via ``max_chars``.
        try:
            max_chars = int(call.args.get("max_chars") or 6000)
        except (TypeError, ValueError):
            max_chars = 6000
        max_chars = max(500, min(max_chars, 20000))

        rows: list[dict[str, Any]] = []
        used_chars = 0
        truncated = False
        for h in hits[:k * 4]:  # 4 = max possible providers
            md = dict(getattr(h, "metadata", None) or {})
            text = (getattr(h, "text", "") or "")[:400]
            # Stop accumulating once budget is exhausted; flag in result.
            if used_chars + len(text) > max_chars and rows:
                truncated = True
                break
            rows.append({
                "id": getattr(h, "id", ""),
                "text": text,
                "ts": getattr(h, "ts", 0.0),
                "kind": md.get("kind") or "?",  # B-197: surface kind
                "provider": md.get("provider") or md.get("backend") or md.get("file") or "?",
                "metadata": md,
            })
            used_chars += len(text)
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "query": query,
                "layer": layer,
                "k": k,
                "mode": used_mode,
                "rows": rows,
                "row_count": len(rows),
                "total_chars": used_chars,
                "truncated": truncated,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _sqlite_query(self, call: ToolCall, t0: float) -> ToolResult:
        """B-37: read-only SQL against the agent's own state DBs.

        Refuses anything that mutates. We use sqlite3's ``authorizer``
        callback as the primary defence: every action the engine
        considers gets vetted, so even sneaky things like
        ``WITH RECURSIVE foo AS (...) DELETE FROM bar`` are caught
        before any rows move. A whitelist statement-prefix check is
        a second belt — keeps obvious garbage out of the parser.

        Connections open with ``mode=ro`` URI so the file itself is
        opened read-only at the OS level too — three layers of
        protection, defensive enough for a tool the LLM can call.
        """
        import sqlite3
        from xmclaw.utils.paths import data_dir

        db_choice = str(call.args.get("db") or "").strip().lower()
        sql = str(call.args.get("sql") or "").strip()
        params_raw = call.args.get("params") or []
        limit = call.args.get("limit")

        # Resolve the DB path (allowlisted).
        if db_choice == "events":
            db_path = data_dir() / "v2" / "events.db"
        elif db_choice == "memory":
            db_path = data_dir() / "v2" / "memory.db"
        else:
            return _fail(
                call, t0,
                f"unknown db {db_choice!r}; expected 'events' or 'memory'",
            )
        if not db_path.is_file():
            return _fail(call, t0, f"db not yet created: {db_path}")

        if not sql:
            return _fail(call, t0, "missing 'sql'")

        # Statement-prefix whitelist. Strip leading comment lines first.
        cleaned_lines = []
        for ln in sql.splitlines():
            s = ln.strip()
            if not s or s.startswith("--"):
                continue
            cleaned_lines.append(ln)
        cleaned = "\n".join(cleaned_lines).strip()
        head = cleaned.split(None, 1)[0].upper() if cleaned else ""
        if head not in ("SELECT", "PRAGMA", "EXPLAIN", "WITH"):
            return _fail(
                call, t0,
                f"only SELECT/PRAGMA/EXPLAIN/WITH allowed (got {head!r})",
            )
        # Reject multi-statement input via stripped trailing-semicolon-aware
        # check — sqlite3 in Python's default mode would only execute the
        # first statement anyway, but we want a clean error.
        body_no_trailing_semi = cleaned.rstrip(";").strip()
        if ";" in body_no_trailing_semi:
            return _fail(
                call, t0,
                "multi-statement input rejected; pass one statement at a time",
            )

        # Coerce params.
        params: tuple = ()
        if isinstance(params_raw, list):
            try:
                params = tuple(params_raw)
            except (TypeError, ValueError):
                return _fail(call, t0, "params must be a list of scalars")

        # Cap row count.
        try:
            n = int(limit) if limit is not None else 50
        except (TypeError, ValueError):
            n = 50
        n = max(1, min(n, 200))

        # Authorizer: deny anything that isn't a pure read.
        ALLOWED_ACTIONS = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_ANALYZE,
            sqlite3.SQLITE_RECURSIVE,
        }

        def _authorizer(action, *_args):  # type: ignore[no-untyped-def]
            if action in ALLOWED_ACTIONS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        # ``mode=ro`` makes the OS-level handle read-only.
        uri = f"file:{db_path}?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as exc:
            return _fail(call, t0, f"open failed: {exc}")

        con.row_factory = sqlite3.Row
        con.set_authorizer(_authorizer)
        try:
            cur = con.execute(cleaned, params)
            rows = cur.fetchmany(n)
            cols = [d[0] for d in (cur.description or [])]
        except sqlite3.Error as exc:
            # B-203: probe data showed 6/11 sqlite_query calls in
            # one audit_pref_kinds turn failed with "no such table:
            # memories" — agent guessed a name that doesn't exist
            # and re-tried multiple times instead of introspecting.
            # When the error is a schema-shape error, surface the
            # actual available tables (or columns of the named
            # table) alongside the error so the next hop has the
            # info to recover without a second tool call.
            err_str = str(exc)
            schema_hint = ""
            try:
                low = err_str.lower()
                # Re-disable authorizer for the meta-query so
                # sqlite_master access is allowed (it's a read,
                # but using the authorizer adds noise here).
                con.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
                if "no such table" in low:
                    meta = con.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' ORDER BY name"
                    ).fetchall()
                    names = [r[0] for r in meta if r[0]]
                    if names:
                        schema_hint = (
                            f" — available tables in '{db_choice}': "
                            f"{', '.join(names)}"
                        )
                    # B-205 cross-tie: if the user is querying memory.db
                    # and the table didn't exist, the question is almost
                    # always semantic ("what does the agent know about X")
                    # — point them at memory_search so they don't keep
                    # guessing schema. This is the recovery path B-205's
                    # prompt change was nudging for; surface it from the
                    # error itself too in case the prompt nudge misses.
                    if db_choice == "memory":
                        schema_hint += (
                            ". For 'what does the agent remember about "
                            "<topic>' queries, use ``memory_search(query, "
                            "kind=?)`` instead of raw SQL — it's faster "
                            "and won't fail with 'no such table'."
                        )
                elif "no such column" in low:
                    # Try to extract the table name from the SQL
                    # ("FROM <table>") to point at its real columns.
                    import re as _re
                    m = _re.search(r"FROM\s+([A-Za-z_][A-Za-z_0-9]*)", cleaned, _re.IGNORECASE)
                    if m:
                        tbl = m.group(1)
                        try:
                            meta = con.execute(
                                f"PRAGMA table_info({tbl})"
                            ).fetchall()
                            names = [r[1] for r in meta if r[1]]
                            if names:
                                schema_hint = (
                                    f" — columns of '{tbl}': "
                                    f"{', '.join(names)}"
                                )
                        except sqlite3.Error:
                            pass
            except sqlite3.Error:
                pass
            return _fail(call, t0, f"query failed: {err_str}{schema_hint}")
        finally:
            con.close()

        out_rows = [dict(r) for r in rows]
        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "db": db_choice,
                "columns": cols,
                "rows": out_rows,
                "row_count": len(out_rows),
                "truncated": len(out_rows) >= n,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _update_persona(self, call: ToolCall, t0: float) -> ToolResult:
        """General-purpose persona file editor — append_section / replace
        / delete on any of the 7 canonical files.

        Per user direction (B-14): full self-modification rights, no
        per-file blocklist. The agent is trusted to use sparingly and
        ask before rewriting SOUL.md / IDENTITY.md.
        """
        file_arg = call.args.get("file")
        mode = call.args.get("mode")
        if not isinstance(file_arg, str) or not file_arg.strip():
            return _fail(call, t0, "missing or empty 'file'")
        if mode not in ("append_section", "replace", "delete"):
            return _fail(call, t0, f"invalid 'mode' {mode!r}; expected append_section|replace|delete")

        canonical = _PERSONA_BASENAMES_LOOKUP.get(file_arg.strip().lower())
        if canonical is None:
            return _fail(
                call, t0,
                f"unknown persona file {file_arg!r}; expected one of "
                + ", ".join(_PERSONA_BASENAMES_LOOKUP.values()),
            )

        try:
            pdir_raw = self._persona_dir_provider()
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"persona dir provider failed: {exc}")
        if pdir_raw is None:
            return _fail(call, t0, "no active persona dir")
        pdir = Path(pdir_raw)
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"could not create persona dir: {exc}")
        target = pdir / canonical

        # B-198 Phase 3: prefer PersonaStore when wired — DB is truth,
        # disk is rendered cache. The store's set_manual handles
        # atomic write + render-to-disk + auto-section preservation.
        # Falls back to legacy direct-markdown writes when no store
        # provider is configured (tests / B-198-disabled installs).
        store = None
        if self._persona_store_provider is not None:
            try:
                store = self._persona_store_provider()
            except Exception:  # noqa: BLE001
                store = None

        # B-63: serialise concurrent writes through the per-path lock
        # so an in-flight append_section (read-modify-write) doesn't
        # race with a sibling delete or replace.
        async with self._fs_lock(target):
            try:
                if mode == "delete":
                    if store is not None:
                        # In B-198 land, "delete" = clear manual row.
                        # Auto-extracted facts are independent — they
                        # stay (use forget_fact / archive separately).
                        await store.set_manual(canonical, "")
                        written_size = 0
                        summary = f"deleted manual portion of {canonical}"
                    else:
                        if target.is_file():
                            target.unlink()
                            written_size = 0
                            summary = f"deleted {canonical}"
                        else:
                            summary = f"{canonical} did not exist (no-op)"
                            written_size = 0
                elif mode == "replace":
                    content = call.args.get("content")
                    if not isinstance(content, str):
                        return _fail(call, t0, "'content' required for replace mode")
                    if store is not None:
                        # store.set_manual strips the auto section
                        # if the caller round-tripped a render — the
                        # manual row stays clean.
                        await store.set_manual(canonical, content)
                        written_size = len(content.encode("utf-8"))
                        summary = (
                            f"replaced manual portion of {canonical} "
                            f"({written_size} bytes)"
                        )
                    else:
                        from xmclaw.utils.fs_locks import atomic_write_text
                        atomic_write_text(target, content)
                        written_size = len(content.encode("utf-8"))
                        summary = (
                            f"replaced {canonical} ({written_size} bytes)"
                        )
                else:  # append_section
                    section = call.args.get("section")
                    content = call.args.get("content")
                    if not isinstance(section, str) or not section.strip():
                        return _fail(call, t0, "'section' required for append_section mode")
                    if not isinstance(content, str) or not content:
                        return _fail(call, t0, "'content' required for append_section mode")
                    section_clean = section.strip().lstrip("#").strip()
                    section_header = f"## {section_clean}"
                    if store is not None:
                        # Read manual portion, append-under-section in
                        # memory, write back. Auto sections are
                        # preserved (rendered fresh by the store).
                        existing_manual = await store.read_manual(canonical)
                        new_manual = _append_under_section(
                            existing_manual,
                            section_header=section_header,
                            bullet=content,
                            placeholder_title=f"{canonical} — agent-curated",
                        )
                        cap = PERSONA_CHAR_CAPS.get(canonical)
                        if cap is not None and len(new_manual) > cap:
                            new_manual = enforce_char_cap(new_manual, cap)
                        await store.set_manual(canonical, new_manual)
                        written_size = len(new_manual.encode("utf-8"))
                    else:
                        existing = (
                            target.read_text(encoding="utf-8")
                            if target.is_file() else ""
                        )
                        new_text = _append_under_section(
                            existing,
                            section_header=section_header,
                            bullet=content,
                            placeholder_title=f"{canonical} — agent-curated",
                        )
                        cap = PERSONA_CHAR_CAPS.get(canonical)
                        if cap is not None and len(new_text) > cap:
                            new_text = enforce_char_cap(new_text, cap)
                        from xmclaw.utils.fs_locks import atomic_write_text
                        atomic_write_text(target, new_text)
                        written_size = len(new_text.encode("utf-8"))
                    summary = f"appended to {canonical} under {section_header}"
            except OSError as exc:
                return _fail(call, t0, f"write failed: {exc}")
            except ValueError as exc:
                # store.set_manual rejects unknown basenames — already
                # validated above via _PERSONA_BASENAMES_LOOKUP, so
                # surface this as an internal error rather than a
                # bad-input fail.
                return _fail(call, t0, f"persona_store rejected: {exc}")

        # Sidecar log so the Memory UI can show "agent wrote this" badges.
        snippet = ""
        if mode == "append_section":
            snippet = (call.args.get("content") or "")[:200]
        elif mode == "replace":
            snippet = (call.args.get("content") or "")[:200]
        elif mode == "delete":
            snippet = "(deleted)"
        self._record_agent_write(
            pdir, canonical,
            call.args.get("section") if mode == "append_section" else None,
            snippet,
        )

        # Trigger system-prompt rebuild on success so the agent's NEXT
        # turn sees its own edit.
        if self._persona_writeback is not None:
            try:
                self._persona_writeback(canonical)
            except Exception:  # noqa: BLE001
                pass

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "file": str(target),
                "mode": mode,
                "summary": summary,
                "bytes": written_size,
            },
            side_effects=(str(target.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _record_agent_write(
        self, pdir: Path, basename: str, section: str | None, snippet: str,
    ) -> None:
        """Record this write to a sidecar log so the Memory page can
        show "agent wrote this" badges. JSONL one row per write.
        Best-effort — sidecar failures don't fail the main write."""
        try:
            sidecar = pdir / ".agent_writes.jsonl"
            entry = {
                "ts": time.time(),
                "file": basename,
                "section": section,
                "snippet": snippet[:200],
            }
            with sidecar.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── B-200 / Phase 5: curriculum-edit proposal flow ──────────────

    async def _propose_curriculum_edit(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-200: file a curriculum (LEARNING.md) edit proposal that
        requires user approval before applying.

        Stores the proposal as ``kind=curriculum_proposal`` in
        memory.db. The user runs ``xmclaw curriculum approve <id>`` /
        ``reject`` to act on it; approve invokes the same store
        write path as ``update_persona`` after applying the edit.
        """
        target_file = str(call.args.get("target_file") or "").strip()
        operation = str(call.args.get("operation") or "").strip()
        section = str(call.args.get("section") or "").strip()
        content = str(call.args.get("content") or "").strip()
        rationale = str(call.args.get("rationale") or "").strip()
        evidence = call.args.get("evidence") or []

        if target_file != "LEARNING.md":
            return _fail(call, t0, "v0 supports target_file=LEARNING.md only")
        if operation != "add_principle":
            return _fail(call, t0, "v0 supports operation=add_principle only")
        if not section:
            return _fail(call, t0, "missing 'section'")
        if not content:
            return _fail(call, t0, "missing 'content'")
        if not rationale or len(rationale) < 20:
            return _fail(
                call, t0,
                "rationale must be at least 20 chars (lazy rationale = "
                "guaranteed rejection)",
            )
        if not isinstance(evidence, list):
            evidence = []

        if self._persona_store_provider is None:
            return _fail(call, t0, "persona_store not wired")
        store = self._persona_store_provider()
        if store is None:
            return _fail(call, t0, "persona_store unavailable at call time")

        # Verify the section actually exists in the current file —
        # propose-anchor must be real or the apply step has nowhere
        # to land the bullet.
        try:
            current_manual = await store.read_manual(target_file)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"read LEARNING.md failed: {exc}")
        if section.startswith("##"):
            section_norm = section
        else:
            section_norm = f"## {section.lstrip('# ').strip()}"
        if section_norm not in current_manual:
            return _fail(
                call, t0,
                f"section {section_norm!r} not found in {target_file}; "
                f"copy a section heading verbatim from the file you "
                f"read at turn start",
            )

        proposal_id = "curriculum_proposal:" + uuid.uuid4().hex
        now = time.time()
        metadata: dict[str, Any] = {
            "kind": "curriculum_proposal",
            "target_file": target_file,
            "operation": operation,
            "section": section_norm,
            "content": content,
            "rationale": rationale,
            "evidence": list(evidence),
            "status": "pending",
            "proposed_by": call.session_id or "agent",
            "proposed_ts": now,
        }

        try:
            await store.add_fact(
                kind="curriculum_proposal",
                text=content,
                metadata=metadata,
                layer="long",
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"proposal write failed: {exc}")

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "proposal_id": proposal_id,
                "status": "pending",
                "target_file": target_file,
                "operation": operation,
                "section": section_norm,
                "review_cmd": "xmclaw curriculum list",
                "approve_cmd": f"xmclaw curriculum approve {proposal_id}",
                "note": (
                    "Proposal queued for user review. "
                    "Will appear in your system prompt only after "
                    "user runs `xmclaw curriculum approve`."
                ),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _list_curriculum_proposals(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """List recent curriculum-edit proposals + their status."""
        status_filter = str(call.args.get("status") or "pending").strip()
        if status_filter not in ("pending", "approved", "rejected", "all"):
            return _fail(call, t0, f"unknown status filter: {status_filter!r}")

        if self._persona_store_provider is None:
            return _fail(call, t0, "persona_store not wired")
        store = self._persona_store_provider()
        if store is None:
            return _fail(call, t0, "persona_store unavailable at call time")

        # Reach into the store's underlying provider to query —
        # PersonaStore doesn't expose "list rows of kind X" yet, but
        # we can use the same memory_provider it's holding.
        mem = getattr(store, "_mem", None)
        if mem is None:
            return _fail(call, t0, "persona_store has no memory provider")

        try:
            hits = await mem.query(
                "long", text=None, k=50,
                filters={"kind": "curriculum_proposal"},
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"proposal query failed: {exc}")

        rows: list[dict[str, Any]] = []
        for h in hits:
            md = getattr(h, "metadata", {}) or {}
            row_status = md.get("status", "pending")
            if status_filter != "all" and row_status != status_filter:
                continue
            rows.append({
                "id": getattr(h, "id", ""),
                "target_file": md.get("target_file"),
                "operation": md.get("operation"),
                "section": md.get("section"),
                "content_preview": (h.text or "")[:200],
                "rationale_preview": (md.get("rationale") or "")[:200],
                "status": row_status,
                "proposed_ts": md.get("proposed_ts"),
                "decided_ts": md.get("decided_ts"),
                "user_reason": md.get("user_reason"),
            })
            if len(rows) >= 20:
                break

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "filter": status_filter,
                "count": len(rows),
                "proposals": rows,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _append_persona(
        self, call: ToolCall, t0: float, *,
        basename: str, section: str, entry: str, placeholder_title: str,
    ) -> ToolResult:
        """Idempotent-ish append: locate or create the ``## section``
        block, append a ``- YYYY-MM-DD: entry`` bullet under it.

        We don't try to be too clever about merging — duplicate entries
        on different days are fine (the date prefix shows when it was
        learned). Heavy de-dup would risk dropping useful context.

        B-63: the read-modify-write block is serialised by a per-path
        asyncio.Lock so concurrent agent + dream cron + multi-agent
        ``remember`` calls don't race + lose appends.
        """
        from datetime import date as _date
        try:
            pdir_raw = self._persona_dir_provider()
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"persona dir provider failed: {exc}")
        if pdir_raw is None:
            return _fail(call, t0, "no active persona dir")
        pdir = Path(pdir_raw)
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(call, t0, f"could not create persona dir: {exc}")
        target = pdir / basename

        # B-198 Phase 3: prefer PersonaStore when wired — write goes
        # through the manual row, store renders disk after. Fallback
        # to legacy direct-markdown writes when no store provider.
        store = None
        if self._persona_store_provider is not None:
            try:
                store = self._persona_store_provider()
            except Exception:  # noqa: BLE001
                store = None

        evicted = 0
        async with self._fs_lock(target):
            today = _date.today().isoformat()
            bullet = f"- {today}: {entry}"
            section_header = f"## {section}"

            if store is not None:
                # Read manual portion, append-under-section in memory,
                # write back. Auto-extracted sections are preserved
                # (rendered from facts on next read).
                try:
                    existing_manual = await store.read_manual(basename)
                except Exception as exc:  # noqa: BLE001
                    return _fail(call, t0, f"store read failed: {exc}")
                new_text = _append_under_section(
                    existing_manual,
                    section_header=section_header,
                    bullet=bullet,
                    placeholder_title=placeholder_title,
                )
                cap = PERSONA_CHAR_CAPS.get(basename)
                if cap is not None and len(new_text) > cap:
                    before_len = len(new_text)
                    new_text = enforce_char_cap(new_text, cap)
                    evicted = before_len - len(new_text)
                try:
                    await store.set_manual(basename, new_text)
                except (OSError, ValueError) as exc:
                    return _fail(call, t0, f"store write failed: {exc}")
            else:
                try:
                    existing = (
                        target.read_text(encoding="utf-8")
                        if target.is_file() else ""
                    )
                except OSError as exc:
                    return _fail(call, t0, f"read failed: {exc}")
                new_text = _append_under_section(
                    existing,
                    section_header=section_header,
                    bullet=bullet,
                    placeholder_title=placeholder_title,
                )
                # B-25: enforce char cap (LRU eviction) — Hermes parity.
                cap = PERSONA_CHAR_CAPS.get(basename)
                if cap is not None and len(new_text) > cap:
                    before_len = len(new_text)
                    new_text = enforce_char_cap(new_text, cap)
                    evicted = before_len - len(new_text)

                from xmclaw.utils.fs_locks import atomic_write_text
                try:
                    atomic_write_text(target, new_text)
                except OSError as exc:
                    return _fail(call, t0, f"write failed: {exc}")

        # Sidecar log: this write came from the agent (vs. user via
        # Memory page). Powers the diff badge in the UI.
        self._record_agent_write(pdir, basename, section, entry)

        # Trigger system-prompt rebuild so the agent's NEXT turn sees the
        # entry in its system prompt (closes the "wrote and then forgot
        # immediately" feedback gap).
        if self._persona_writeback is not None:
            try:
                self._persona_writeback(basename)
            except Exception:  # noqa: BLE001 — writeback failure must
                # not roll back the write itself
                pass

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "file": str(target),
                "section": section,
                "appended": bullet,
                "bytes": len(new_text.encode("utf-8")),
                "evicted_chars": evicted,
            },
            side_effects=(str(target.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── allowlist ─────────────────────────────────────────────────────

    def _fs_lock(self, path: Path) -> asyncio.Lock:
        """B-63 / B-65: per-path async lock for read-modify-write.

        B-65: routes through ``xmclaw.utils.fs_locks.get_lock`` so the
        SAME lock is shared with BuiltinFileMemoryProvider and
        DreamCompactor — without this, three writers to MEMORY.md each
        held their own mutex providing zero actual mutual exclusion.
        """
        from xmclaw.utils.fs_locks import get_lock
        return get_lock(path)

    def _check_allowed(self, path: Path) -> None:
        if self._allowed is None:
            return
        resolved = path.resolve()
        for allowed in self._allowed:
            try:
                resolved.relative_to(allowed)
                return
            except ValueError:
                continue
        raise PermissionError(
            f"path {resolved} is outside the sandbox allowlist {self._allowed}"
        )

    def _audit_workspace_containment(self, path: Path, op: str) -> None:
        """B-331: log + emit a visibility signal when a write-path
        op targets a path outside every configured workspace root.

        Visibility-only — does NOT raise. Agents still write
        successfully; the signal is for daemon.log auditors who want
        to spot the agent escaping the workspace boundaries the user
        configured via the Web UI. ASK-confirm / deny is a separate
        UX-design epic.

        No-op when ``workspace_manager_provider`` isn't wired (tests,
        echo-mode), when there are zero configured roots (fresh
        install), or when the path lives inside a configured root
        (the happy case).
        """
        if self._workspace_manager_provider is None:
            return
        try:
            mgr = self._workspace_manager_provider()
        except Exception:  # noqa: BLE001 — provider is best-effort
            return
        if mgr is None:
            return
        try:
            roots = mgr.get().roots
        except Exception:  # noqa: BLE001
            return
        if not roots:
            # No workspace configured yet — pre-onboard / fresh install.
            # Don't spam the log on every write before the user has
            # picked a workspace.
            return
        # Reuse the manager's containment helper so the matching logic
        # stays in one place (cline parity, see WorkspaceManager).
        try:
            root = mgr.resolve_path_to_root(path)
        except Exception:  # noqa: BLE001
            return
        if root is not None:
            return
        # Outside every configured root — log + emit.
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        roots_repr = [str(r.path) for r in roots]
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "tool.write_outside_workspace op=%s path=%s "
            "configured_roots=%s "
            "note=advisory_only_no_runtime_block",
            op, resolved, roots_repr,
        )

    # ── voice tools (B-388) ───────────────────────────────────────────

    async def _voice_transcribe(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-388: hand audio bytes to the wired STT provider.

        Accepts EXACTLY ONE of ``audio_path`` (filesystem path) or
        ``audio_b64`` (base64-encoded). Both → reject (the caller's
        intent is ambiguous). Neither → reject (need an input).
        """
        import base64
        import json
        args = call.args or {}
        audio_path = args.get("audio_path")
        audio_b64 = args.get("audio_b64")
        has_path = isinstance(audio_path, str) and audio_path
        has_b64 = isinstance(audio_b64, str) and audio_b64
        if has_path and has_b64:
            return _fail(
                call, t0,
                "voice_transcribe accepts exactly one of audio_path / audio_b64",
            )
        if not (has_path or has_b64):
            return _fail(call, t0, "voice_transcribe needs an audio source")

        if has_path:
            try:
                p = Path(audio_path).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                return _fail(call, t0, f"audio_path resolve failed: {exc}")
            try:
                audio_bytes = p.read_bytes()
            except FileNotFoundError:
                return _fail(call, t0, f"audio file not found: {audio_path}")
            except PermissionError as exc:
                return _fail(call, t0, f"permission denied: {exc}")
            source = "audio_path"
        else:
            try:
                audio_bytes = base64.b64decode(audio_b64, validate=False)
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"audio_b64 decode failed: {exc}")
            source = "audio_b64"

        try:
            text = await self._stt_provider.transcribe(audio_bytes)  # type: ignore[union-attr]
        except ImportError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

        payload = json.dumps(
            {"text": text, "audio_bytes": len(audio_bytes), "source": source},
            ensure_ascii=False,
        )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=payload,
            error=None,
            latency_ms=(time.monotonic() - t0) * 1000.0,
            side_effects=(),
        )

    async def _voice_synthesize(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """B-388: text → mp3 via the wired TTS provider.

        Writes the result to ``$XMC_DATA_DIR/v2/audio/<uuid>.mp3`` and
        records the resolved path on ``side_effects`` so the grader can
        verify the write actually landed.
        """
        import json
        import os
        import uuid
        args = call.args or {}
        text = args.get("text")
        voice = args.get("voice", "default")
        if not isinstance(text, str):
            return _fail(call, t0, "voice_synthesize: 'text' must be a string")
        if not isinstance(voice, str):
            voice = "default"

        try:
            audio_bytes = await self._tts_provider.synthesize(text, voice=voice)  # type: ignore[union-attr]
        except ImportError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

        # Patch A (2026-05-10): paths.data_dir() (avoids duplicating
        # the XMC_DATA_DIR fallback inline — same logic as paths.py).
        from xmclaw.utils.paths import data_dir as _xmc_data_dir
        audio_dir = _xmc_data_dir() / "v2" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / f"{uuid.uuid4().hex}.mp3"
        audio_path.write_bytes(audio_bytes)

        payload = json.dumps(
            {"audio_path": str(audio_path), "bytes": len(audio_bytes)},
            ensure_ascii=False,
        )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=payload,
            error=None,
            latency_ms=(time.monotonic() - t0) * 1000.0,
            side_effects=(f"wrote audio to {audio_path.resolve()}",),
        )


