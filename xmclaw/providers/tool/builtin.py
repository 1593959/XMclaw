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
from pathlib import Path

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# ── specs ──────────────────────────────────────────────────────────────

_FILE_READ_SPEC = ToolSpec(
    name="file_read",
    description="Read a UTF-8 text file and return its full contents.",
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
        },
        "required": ["path"],
    },
)

_FILE_WRITE_SPEC = ToolSpec(
    name="file_write",
    description=(
        "Write UTF-8 text to a file, creating parent directories as needed. "
        "Overwrites existing files."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path."},
            "content": {"type": "string", "description": "Text to write."},
        },
        "required": ["path", "content"],
    },
)

_LIST_DIR_SPEC = ToolSpec(
    name="list_dir",
    description=(
        "List entries in a directory. Returns a JSON-ish text block with "
        "one entry per line: '<type> <size> <name>' where type is 'd' for "
        "directories, 'f' for files, or 'l' for symlinks."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute directory path."},
            "pattern": {
                "type": "string",
                "description": "Optional glob filter (e.g. '*.docx'). Default '*' (all).",
            },
        },
        "required": ["path"],
    },
)

_APPLY_PATCH_SPEC = ToolSpec(
    name="apply_patch",
    description=(
        "Apply one or more in-place edits to a single text file atomically. "
        "Each edit replaces an exact ``old_text`` block with ``new_text``. "
        "Every ``old_text`` must occur EXACTLY ONCE in the file at the time "
        "the patch runs — if zero or multiple matches are found, the whole "
        "patch aborts and nothing is written. Prefer this over file_write "
        "when you only want to change a few lines: it preserves the rest "
        "of the file verbatim and refuses to clobber an unexpected state. "
        "Use file_read first to grab the exact ``old_text`` (whitespace "
        "matters)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path."},
            "edits": {
                "type": "array",
                "description": "List of {old_text, new_text} edits applied in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to find. Must occur exactly once.",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text. May be empty to delete.",
                        },
                    },
                    "required": ["old_text", "new_text"],
                },
                "minItems": 1,
            },
        },
        "required": ["path", "edits"],
    },
)

_BASH_SPEC = ToolSpec(
    name="bash",
    description=(
        "Run a shell command on the local machine and return combined "
        "stdout+stderr plus the exit code. Use for directory listings, "
        "finding files, git status, etc. Be careful with destructive "
        "commands -- there is no undo."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Kill after N seconds. Default 30.",
            },
        },
        "required": ["command"],
    },
)

_WEB_FETCH_SPEC = ToolSpec(
    name="web_fetch",
    description=(
        "GET a URL and return its response body as text (up to 200 KB). "
        "Follows redirects. Use when the user asks about a specific "
        "web page."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full http(s) URL."},
            "max_chars": {
                "type": "integer",
                "description": "Truncation cap. Default 200000.",
            },
        },
        "required": ["url"],
    },
)

_WEB_SEARCH_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Search the web via DuckDuckGo's HTML endpoint (no API key). "
        "Returns the top results as 'TITLE\\nURL\\nSNIPPET' blocks. "
        "Use for factual lookups where a fresh page is needed."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": "Top-N cap. Default 5.",
            },
        },
        "required": ["query"],
    },
)

_TODO_WRITE_SPEC = ToolSpec(
    name="todo_write",
    description=(
        "Record the current plan for a multi-step task as a todo list. "
        "Each item has a 'content' and 'status' (pending|in_progress|done). "
        "Overwrites the full list; call again with updated statuses as "
        "work progresses. The user sees a live 'Todos' panel that mirrors "
        "this state."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Ordered list of todo items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done"],
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["items"],
    },
)

_TODO_READ_SPEC = ToolSpec(
    name="todo_read",
    description=(
        "Read back the current todo list for this session. Use this "
        "before updating statuses to make sure nothing was missed."
    ),
    parameters_schema={"type": "object", "properties": {}},
)

_REMEMBER_SPEC = ToolSpec(
    name="remember",
    description=(
        "Append a durable, cross-session note to MEMORY.md. Use sparingly "
        "for facts that will still matter NEXT conversation: project "
        "conventions, decisions made, recurring constraints, things the "
        "user explicitly told you to remember. NOT for ephemeral session "
        "context (use todos for that). NOT for facts about the user as a "
        "person (use learn_about_user for that). Each call appends a "
        "timestamped bullet under the matching ## category heading; the "
        "category is created if missing. Categories should be short noun "
        "phrases like 'Project conventions' / 'User preferences' / "
        "'Decisions'. Effect lands on the next turn — your system prompt "
        "is rebuilt the moment this returns."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Short heading the note belongs under "
                "(e.g. 'Project conventions', 'Decisions'). Will be "
                "created if it doesn't exist yet.",
            },
            "note": {
                "type": "string",
                "description": "The fact to remember, one sentence "
                "ideally. Will be prefixed with the current date.",
            },
        },
        "required": ["category", "note"],
    },
)

_LEARN_ABOUT_USER_SPEC = ToolSpec(
    name="learn_about_user",
    description=(
        "Append a fact about the user to USER.md. Use when you learn "
        "something durable about who they are or how they want to work: "
        "their role, expertise, language preferences, communication "
        "style, recurring projects, things they've corrected you on. "
        "Skip noise (one-off requests, things that change session-to-"
        "session — those go to todos). Each call appends a timestamped "
        "bullet under the matching ## section; sections are created on "
        "demand. Effect lands on the next turn — your system prompt is "
        "rebuilt the moment this returns."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": "Section heading (e.g. 'Role & expertise', "
                "'Communication style', 'Preferences'). Created if missing.",
            },
            "fact": {
                "type": "string",
                "description": "The fact, one sentence. Will be "
                "prefixed with the current date.",
            },
        },
        "required": ["section", "fact"],
    },
)

_SCHEDULE_FOLLOWUP_SPEC = ToolSpec(
    name="schedule_followup",
    description=(
        "Schedule a future agent turn — your own reminder system. Use "
        "when the user asks you to follow up later (\"remind me "
        "tomorrow morning\"), or when YOU decide a periodic check is "
        "useful (e.g. \"remember to revisit MEMORY.md weekly\"). "
        "Creates a cron job under ``~/.xmclaw/cron/jobs.json``; on "
        "fire the daemon spins up a fresh session named "
        "``cron:<job_id>:<ts>`` and runs the prompt as you. The job "
        "fires recurrently per ``schedule``; use ``run_once=true`` "
        "for one-shot reminders. The user can pause/resume/delete the "
        "job from the Cron page in the UI."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short label, shown in the UI cron list. "
                "e.g. \"daily standup nudge\".",
            },
            "schedule": {
                "type": "string",
                "description": "When to fire. Two formats accepted: "
                "\"every Nu\" (interval — e.g. \"every 5m\", \"every "
                "1d\", \"every 2h\") OR full cron syntax (\"0 9 * * "
                "MON-FRI\" means 9 AM weekdays — needs croniter "
                "installed). For one-off reminders, set run_once=true "
                "and use the smallest interval that gets you past the "
                "trigger time (e.g. \"every 1h\" run-once will fire "
                "within an hour).",
            },
            "prompt": {
                "type": "string",
                "description": "What the FUTURE you should do when the "
                "job fires. Write it like a self-instruction: \"Check "
                "the user's MEMORY.md for stale entries and report\". "
                "The future session has no chat history; restate any "
                "context you need.",
            },
            "run_once": {
                "type": "boolean",
                "description": "If true, the job auto-deletes after "
                "firing once. Default false (recurring).",
            },
        },
        "required": ["name", "schedule", "prompt"],
    },
)

_UPDATE_PERSONA_SPEC = ToolSpec(
    name="update_persona",
    description=(
        "Edit ANY of your own persona files. This is the powerful "
        "self-modification tool — use it when ``remember`` / "
        "``learn_about_user`` are too narrow. Targets one of the 7 "
        "canonical files: SOUL.md, AGENTS.md, IDENTITY.md, USER.md, "
        "TOOLS.md, BOOTSTRAP.md, MEMORY.md. Three modes:\n\n"
        "  • ``append_section`` — add a bullet (or arbitrary block) "
        "under a section header. Args: section, content. The most "
        "common mode.\n"
        "  • ``replace`` — overwrite the entire file with ``content``. "
        "Use sparingly; this discards prior state. Good for SOUL/"
        "IDENTITY rewrites the user explicitly asked for, or for "
        "cleaning up MEMORY.md after a refactor.\n"
        "  • ``delete`` — remove the file from disk. Used for the "
        "BOOTSTRAP.md cleanup after first-run interview completes "
        "(write IDENTITY/USER, then delete BOOTSTRAP). DO NOT delete "
        "SOUL/AGENTS/USER/MEMORY/IDENTITY — they have no opt-in/opt-"
        "out semantics.\n\n"
        "Be conservative with SOUL.md and IDENTITY.md — those are the "
        "user's mental model of you; only modify if the user has "
        "explicitly asked you to. MEMORY.md and USER.md are yours to "
        "curate within reason. Effect lands on the next turn — your "
        "system prompt is rebuilt immediately on success."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "One of: SOUL.md, AGENTS.md, IDENTITY.md, "
                "USER.md, TOOLS.md, BOOTSTRAP.md, MEMORY.md. "
                "Case-insensitive.",
            },
            "mode": {
                "type": "string",
                "enum": ["append_section", "replace", "delete"],
                "description": "How to mutate the file.",
            },
            "section": {
                "type": "string",
                "description": "Section heading for append_section mode "
                "(e.g. '## Decisions' — '## ' prefix optional). Ignored "
                "by other modes.",
            },
            "content": {
                "type": "string",
                "description": "Content to write. For append_section: "
                "the block to append (one bullet, multiple bullets, "
                "or a paragraph). For replace: the full new file body. "
                "Ignored for delete.",
            },
        },
        "required": ["file", "mode"],
    },
)


_MAX_WEB_BYTES = 200_000
_BASH_DEFAULT_TIMEOUT = 30.0
_BASH_MAX_OUTPUT = 100_000
_VALID_TODO_STATUSES = {"pending", "in_progress", "done"}

# Map case-insensitive lookup → canonical-cased basename. Used by
# the ``update_persona`` tool so the LLM can pass "soul.md", "SOUL",
# or "Soul.md" and we resolve to the on-disk filename.
_PERSONA_BASENAMES_LOOKUP: dict[str, str] = {}
for _b in (
    "AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md",
    "TOOLS.md", "BOOTSTRAP.md", "MEMORY.md",
):
    _PERSONA_BASENAMES_LOOKUP[_b.lower()] = _b
    _PERSONA_BASENAMES_LOOKUP[_b.lower().removesuffix(".md")] = _b


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
        persona_dir_provider: "object | None" = None,
        persona_writeback: "object | None" = None,
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
        # Optional callable () -> Path | None returning the daemon's
        # active workspace root (driven by ~/.xmclaw/state.json via
        # WorkspaceManager). When the LLM omits an explicit `cwd` arg
        # on a bash call we fall back to this so commands like `ls` /
        # `pwd` run inside the project the user is actually working
        # on, not wherever the daemon was started from.
        self._workspace_root_provider = workspace_root_provider
        # Per-session todo lists. Key: session_id (falls back to "_default"
        # when a caller doesn't fill in ToolCall.session_id).
        self._todos: dict[str, list[dict[str, str]]] = {}
        # Optional callback fired on every todo_write so the agent loop /
        # daemon can emit a TODO_UPDATED event to the bus. Signature:
        # ``def todo_listener(session_id, items) -> None``. Keeping it as
        # a plain callable avoids coupling this module to the bus type.
        self._todo_listener = todo_listener

    def list_tools(self) -> list[ToolSpec]:
        specs = [_FILE_READ_SPEC, _FILE_WRITE_SPEC, _APPLY_PATCH_SPEC, _LIST_DIR_SPEC]
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
        # schedule_followup is always available — it doesn't need any
        # constructor wiring; the cron store is a process-wide singleton.
        specs.append(_SCHEDULE_FOLLOWUP_SPEC)
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
            if call.name == "schedule_followup":
                return await self._schedule_followup(call, t0)
            return _fail(call, t0, f"unknown tool: {call.name!r}")
        except PermissionError as exc:
            return _fail(call, t0, f"permission denied: {exc}")
        except FileNotFoundError as exc:
            return _fail(call, t0, f"file not found: {exc}")
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")

    # ── filesystem tools ──────────────────────────────────────────────

    async def _file_read(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        path = Path(raw_path)
        self._check_allowed(path)
        content = path.read_text(encoding="utf-8")
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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
                return _fail(
                    call, t0,
                    f"edits[{i}].old_text not found in {path} — "
                    f"file may have changed; re-read it before patching",
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

    async def _list_dir(self, call: ToolCall, t0: float) -> ToolResult:
        raw_path = call.args.get("path")
        pattern = call.args.get("pattern", "*")
        if not isinstance(raw_path, str) or not raw_path:
            return _fail(call, t0, "missing or empty 'path' argument")
        if not isinstance(pattern, str) or not pattern:
            pattern = "*"
        path = Path(raw_path)
        self._check_allowed(path)
        if not path.exists():
            return _fail(call, t0, f"path does not exist: {path}")
        if not path.is_dir():
            return _fail(call, t0, f"not a directory: {path}")
        lines: list[str] = []
        for entry in sorted(path.glob(pattern)):
            kind = "l" if entry.is_symlink() else (
                "d" if entry.is_dir() else "f"
            )
            try:
                size = entry.stat().st_size if kind == "f" else 0
            except OSError:
                size = 0
            lines.append(f"{kind} {size:>10} {entry.name}")
        body = "\n".join(lines) if lines else f"(no entries matching {pattern!r})"
        return ToolResult(
            call_id=call.id, ok=True,
            content=f"{len(lines)} entries in {path}:\n{body}",
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

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
            return _fail(call, t0, f"http error: {exc}")
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
            return _fail(call, t0, f"search error: {exc}")
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

        # Append a self-cleanup line for run_once jobs. We don't have
        # a delete-self tool, but the future agent can just call
        # bash/curl against /api/v2/cron/{job_id}. Cleaner: implement
        # one-shot semantics in the cron store later. For now, leave a
        # clear breadcrumb in the prompt so the future agent knows.
        full_prompt = prompt.strip()
        if run_once:
            full_prompt += (
                "\n\n[note: this is a one-shot reminder. After "
                "responding, you may delete this job via the Cron page "
                "or by calling DELETE /api/v2/cron/{this_job_id}.]"
            )

        try:
            from xmclaw.core.scheduler.cron import CronJob, default_cron_store
            store = default_cron_store()
            import uuid as _uuid
            job = CronJob(
                id=_uuid.uuid4().hex,
                name=name.strip(),
                schedule=schedule.strip(),
                prompt=full_prompt,
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

        try:
            if mode == "delete":
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
                target.write_text(content, encoding="utf-8")
                written_size = len(content.encode("utf-8"))
                summary = f"replaced {canonical} ({written_size} bytes)"
            else:  # append_section
                section = call.args.get("section")
                content = call.args.get("content")
                if not isinstance(section, str) or not section.strip():
                    return _fail(call, t0, "'section' required for append_section mode")
                if not isinstance(content, str) or not content:
                    return _fail(call, t0, "'content' required for append_section mode")
                section_clean = section.strip().lstrip("#").strip()
                section_header = f"## {section_clean}"
                existing = (
                    target.read_text(encoding="utf-8") if target.is_file() else ""
                )
                new_text = _append_under_section(
                    existing,
                    section_header=section_header,
                    bullet=content,  # caller decides whether to lead with "-"
                    placeholder_title=f"{canonical} — agent-curated",
                )
                target.write_text(new_text, encoding="utf-8")
                written_size = len(new_text.encode("utf-8"))
                summary = f"appended to {canonical} under {section_header}"
        except OSError as exc:
            return _fail(call, t0, f"write failed: {exc}")

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

    async def _append_persona(
        self, call: ToolCall, t0: float, *,
        basename: str, section: str, entry: str, placeholder_title: str,
    ) -> ToolResult:
        """Idempotent-ish append: locate or create the ``## section``
        block, append a ``- YYYY-MM-DD: entry`` bullet under it.

        We don't try to be too clever about merging — duplicate entries
        on different days are fine (the date prefix shows when it was
        learned). Heavy de-dup would risk dropping useful context.
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

        try:
            existing = (
                target.read_text(encoding="utf-8") if target.is_file() else ""
            )
        except OSError as exc:
            return _fail(call, t0, f"read failed: {exc}")

        today = _date.today().isoformat()
        bullet = f"- {today}: {entry}"
        section_header = f"## {section}"

        new_text = _append_under_section(
            existing,
            section_header=section_header,
            bullet=bullet,
            placeholder_title=placeholder_title,
        )

        try:
            target.write_text(new_text, encoding="utf-8")
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
            },
            side_effects=(str(target.resolve()),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # ── allowlist ─────────────────────────────────────────────────────

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


# ── helpers ───────────────────────────────────────────────────────────

_BULLET_DATE_RE = re.compile(
    r"^\s*-\s*\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?(?:\s+[A-Z]{2,5})?\s*[:：]?\s*"
)


def _bullet_core(line: str) -> str:
    """Extract the meat of a bullet for dedup comparison.

    Strips ``- YYYY-MM-DD: `` (and variants with time / TZ) plus
    surrounding whitespace, then lowercases and collapses internal
    whitespace. Two bullets compare equal iff they say the same thing
    regardless of when they were written.
    """
    cleaned = _BULLET_DATE_RE.sub("", line.strip())
    # Some entries got nested ``YYYY-MM-DD: YYYY-MM-DD: ...`` from
    # earlier dedup-less runs — strip a second date prefix too.
    cleaned = _BULLET_DATE_RE.sub("", cleaned).strip()
    # Normalise punctuation/whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).lower()
    # Strip trailing punctuation that doesn't change semantics.
    return cleaned.rstrip(".。,，!！?？")


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
    incoming_core = _bullet_core(bullet)
    if incoming_core:
        for ln in lines:
            stripped = ln.strip()
            if not stripped or not stripped.startswith("-"):
                continue
            if _bullet_core(stripped) == incoming_core:
                # Already there — return file unchanged.
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
            except Exception:
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
