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
import time
from pathlib import Path

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.builtin_user import (
    _PENDING_QUESTIONS,
    _PENDING_QUESTION_PAYLOADS,
)

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

from xmclaw.providers.tool.builtin_db import BuiltinToolsDbMixin
from xmclaw.providers.tool.builtin_fs import BuiltinToolsFsMixin
from xmclaw.providers.tool.builtin_memory import BuiltinToolsMemoryMixin
from xmclaw.providers.tool.builtin_persona import BuiltinToolsPersonaMixin
from xmclaw.providers.tool.builtin_shell import BuiltinToolsShellMixin
from xmclaw.providers.tool.builtin_user import BuiltinToolsUserMixin
from xmclaw.providers.tool.builtin_voice import BuiltinToolsVoiceMixin
from xmclaw.providers.tool.builtin_worktree import BuiltinToolsWorktreeMixin


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


def list_pending_questions() -> list[dict]:
    """B-99: return snapshots of every in-flight question."""
    return list(_PENDING_QUESTION_PAYLOADS.values())


def resolve_pending_question(
    question_id: str, answer: "str | list[str]",
) -> bool:
    """Resolve an in-flight ``ask_user_question`` future."""
    fut = _PENDING_QUESTIONS.get(question_id)
    if fut is None or fut.done():
        return False
    fut.set_result(answer)
    return True

class BuiltinTools(
    BuiltinToolsDbMixin,
    BuiltinToolsFsMixin,
    BuiltinToolsMemoryMixin,
    BuiltinToolsPersonaMixin,
    BuiltinToolsShellMixin,
    BuiltinToolsUserMixin,
    BuiltinToolsVoiceMixin,
    BuiltinToolsWorktreeMixin,
    ToolProvider,
):
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
