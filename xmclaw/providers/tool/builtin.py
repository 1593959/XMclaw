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


# ── specs ──────────────────────────────────────────────────────────────

_FILE_READ_SPEC = ToolSpec(
    name="file_read",
    description=(
        "Read a UTF-8 text file. Defaults to the first ~100KB; use "
        "``offset`` + ``limit`` (1-indexed lines) to read a range, or "
        "``max_bytes`` to widen the cap up to 1MB. Refuses files that "
        "look binary (NUL byte in the first 8KB).\n\n"
        "Result is text. When the file was truncated by the cap, the "
        "result ends with a ``[truncated, N total bytes]`` marker so "
        "the agent knows there's more."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "1-indexed line number to start at "
                "(default 1). When set, ``limit`` defaults to 2000.",
            },
            "limit": {
                "type": "integer",
                "description": "Number of lines to read after "
                "``offset``. Default 2000 when ``offset`` is set, "
                "else read up to ``max_bytes`` worth.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Byte cap (default 100000, max 1000000). "
                "Ignored when ``offset``/``limit`` is set.",
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
        "List entries in a directory. Returns a text block with one "
        "entry per line: '<type> <size> <name>' where type is 'd' for "
        "directories, 'f' for files, or 'l' for symlinks. Capped at "
        "``limit`` entries (default 200, max 5000) to keep large "
        "directories from flooding context — append a '[truncated, N "
        "more]' marker when the cap kicks in."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute directory path."},
            "pattern": {
                "type": "string",
                "description": "Optional glob filter (e.g. '*.docx'). Default '*' (all).",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries returned. Default 200, max 5000.",
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

_GLOB_FILES_SPEC = ToolSpec(
    name="glob_files",
    description=(
        "Find files matching a glob pattern. Cross-platform — no "
        "shell required, works on Windows where ``find`` /  ``ls`` "
        "may be unavailable. Pattern uses ``**`` for recursive "
        "match (e.g. ``src/**/*.py``)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern. Supports ``**`` for recursive match.",
            },
            "root": {
                "type": "string",
                "description": "Directory to search from. Default: current workspace.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results. Default 200, max 2000.",
            },
        },
        "required": ["pattern"],
    },
)


_GREP_FILES_SPEC = ToolSpec(
    name="grep_files",
    description=(
        "Search file contents for a regex pattern across one or "
        "more files. Cross-platform stdlib re — no ``grep`` / "
        "``rg`` binary needed. Returns line-level hits with file "
        "path + line number + matching line. Use ``glob`` to "
        "filter the search corpus."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python regex. Use plain text for "
                "literal substring; metachars are honoured.",
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern picking which files to "
                "search. Default ``**/*`` (everything under root). "
                "Common: ``**/*.py`` / ``src/**/*.ts``.",
            },
            "root": {
                "type": "string",
                "description": "Directory to search from. Default: current workspace.",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Match case-insensitively. Default false.",
            },
            "max_hits": {
                "type": "integer",
                "description": "Cap on total matches returned. Default 200.",
            },
        },
        "required": ["pattern"],
    },
)


_FILE_DELETE_SPEC = ToolSpec(
    name="file_delete",
    description=(
        "Delete a file or empty directory. Cross-platform — no "
        "shell needed. Refuses non-empty directories (use "
        "``recursive=true`` to allow). Refuses paths outside any "
        "configured ``allowed_dirs`` sandbox.\n\n"
        "Use sparingly. Agent self-modification of source code "
        "should normally use ``apply_patch`` to remove content; "
        "``file_delete`` is for genuinely-stale artifacts (old "
        "log files, scratch dirs, completed scaffolds)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or workspace-relative path.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Allow deleting non-empty directories. "
                "Default false.",
            },
        },
        "required": ["path"],
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

_MEMORY_SEARCH_SPEC = ToolSpec(
    name="memory_search",
    description=(
        "Search the agent's long-term memory across every wired "
        "backend (persona files MEMORY.md/USER.md, sqlite-vec vector "
        "store, optional cloud providers like hindsight/supermemory/"
        "mem0). Use this BEFORE answering questions about prior "
        "decisions, user preferences, names/dates the user mentioned, "
        "or anything that might already be on disk from earlier "
        "sessions.\n\n"
        "Hits are merged across providers — the external (vector) "
        "provider's results come first when present, builtin "
        "(keyword) bullets fill in. Each row carries the originating "
        "provider in metadata so you can tell."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query. CJK / Latin "
                "both fine.",
            },
            "k": {
                "type": "integer",
                "description": "Top-k hits per provider (default 5, max 20).",
            },
            "layer": {
                "type": "string",
                "enum": ["short", "working", "long"],
                "description": "Memory layer (default 'long').",
            },
            "max_chars": {
                "type": "integer",
                "description": "Total result chars cap (default 6000, "
                "min 500, max 20000). Returns ``truncated: true`` when "
                "the cap stops accumulation before all hits land.",
            },
        },
        "required": ["query"],
    },
)


_MEMORY_PIN_SPEC = ToolSpec(
    name="memory_pin",
    description=(
        "Pin a fact to MEMORY.md's `## Pinned` section. Pinned items "
        "survive every Auto-Dream pass — the dream prompt preserves "
        "the section verbatim. Use for facts you NEVER want to lose: "
        "credentials format hints, irreversible decisions, the user's "
        "absolute preferences (\"never auto-push to main\"), recovery "
        "procedures.\n\n"
        "Distinct from regular ``remember``: ``remember`` lands in a "
        "topical section that Auto-Dream may dedupe / consolidate. "
        "``memory_pin`` lands somewhere safe.\n\n"
        "After pin, the indexer (10s) will embed it like any other "
        "MEMORY.md content, so memory_search still finds it."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact, one sentence. Will be "
                "prefixed with the current date.",
            },
        },
        "required": ["content"],
    },
)


_MEMORY_COMPACT_SPEC = ToolSpec(
    name="memory_compact",
    description=(
        "Trigger an Auto-Dream pass on MEMORY.md right now (instead of "
        "waiting until 03:00 daily). Useful when you've just done a "
        "burst of remember/update_persona writes and want to dedupe + "
        "crystallise BEFORE the next conversation.\n\n"
        "The compactor reads MEMORY.md + last 7 days of memory/*.md "
        "logs, asks the LLM to merge duplicates / overwrite stale "
        "facts / consolidate / drop expired, writes a backup under "
        "<persona>/backup/ first, then rewrites MEMORY.md atomically.\n\n"
        "Returns {ok, before_chars, after_chars, saved_chars, "
        "backup_path}. Refuses to run when no LLM is configured. "
        "Refuses rewrites that shrink the file by more than 70% as "
        "an LLM-error guard. The pre-compact version is recoverable "
        "from the backup directory if the rewrite went badly."
    ),
    parameters_schema={
        "type": "object",
        "properties": {},
    },
)


_AGENT_STATUS_SPEC = ToolSpec(
    name="agent_status",
    description=(
        "Self-introspection — returns the daemon's current state in "
        "one shot: indexer (running? last tick? chunks indexed?), "
        "cron (job count + next fire), memory layer (provider list, "
        "vector count when known), config snapshot.\n\n"
        "Use BEFORE answering questions like 'are you indexing my "
        "notes?', 'what cron jobs are scheduled?', 'is the evolution "
        "subsystem running?'. Pure read — no side effects.\n\n"
        "Returns a structured dict; the agent should summarise the "
        "interesting subset rather than dumping the whole thing back "
        "to the user."
    ),
    parameters_schema={
        "type": "object",
        "properties": {},
    },
)


_NOTE_WRITE_SPEC = ToolSpec(
    name="note_write",
    description=(
        "Write or update a topic note under ~/.xmclaw/memory/*.md — "
        "the user's Notes tab in the Memory page. Use this when YOU "
        "(the agent) want to capture a workflow, a lesson learned, a "
        "process improvement, a piece of accumulated reference, or a "
        "draft you want to revisit. The Web UI lists every file here "
        "as a 笔记 entry the user can browse + edit.\n\n"
        "This is one of your evolution surfaces. Examples:\n"
        "  • workflow.md — \"how I usually approach X kind of task\"\n"
        "  • lessons-2026-04.md — failures + what to do differently\n"
        "  • api-cheatsheet.md — accumulated reference for an API\n\n"
        "When ``mode='replace'`` the file is overwritten with "
        "``content``. When ``mode='append'`` the content is appended "
        "after a separator. Default ``replace``.\n\n"
        "**Strongly recommended (B-93):** pass a one-line "
        "``description`` field describing what this note covers. The "
        "LLM-picker uses descriptions to choose which notes to recall "
        "at the start of a turn — a note with no description is "
        "harder to find. ``tags`` is optional but helps cluster "
        "related notes in the manifest.\n\n"
        "After write the indexer (10s poll) embeds the file into the "
        "vector store, so future ``memory_search`` calls can retrieve "
        "it semantically."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Note filename. ``.md`` auto-appended. "
                "Slashes are stripped for safety.",
            },
            "content": {
                "type": "string",
                "description": "Markdown body to write. Headings + "
                "bullet lists are recommended for retrievability.",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "description": "Write mode. Default 'replace'.",
            },
            "description": {
                "type": "string",
                "description": "B-93: one-line summary. Stored as "
                "frontmatter ``description:`` so the LLM-picker can "
                "find this note by intent, not just by keyword. "
                "Skip on append mode — keeps existing header.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "B-93: short tag list for clustering. "
                "Stored as frontmatter ``tags: [a, b, c]``. Optional.",
            },
        },
        "required": ["name", "content"],
    },
)


_RECALL_USER_PREFS_SPEC = ToolSpec(
    name="recall_user_preferences",
    description=(
        "Look up auto-extracted user preferences (USER.md `## "
        "Auto-extracted preferences` section) — the rolling delta "
        "log written by ProfileExtractor as you converse. Each entry "
        "carries a kind (preference/constraint/style/habit), the "
        "natural-language text, an LLM-estimated confidence, and the "
        "source session id.\n\n"
        "Use BEFORE making style / format / tool-choice decisions "
        "you might be wrong about. Example trigger thoughts:\n"
        "  • 'should I write this in Markdown or plain text?' → "
        "recall topic='format'\n"
        "  • 'should I run this command without asking?' → recall "
        "topic='constraint'\n"
        "  • 'what's the user's preferred git workflow?' → recall "
        "topic='git'\n\n"
        "USER.md is already in your system prompt — this tool is for "
        "the cases where you want a focused subset filtered by topic, "
        "not a wholesale re-read. Returns [] cleanly when no "
        "auto-extracted entries exist (fresh install / extractor "
        "disabled)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Substring filter on entry text "
                "(case-insensitive). Omit for everything.",
            },
            "kind": {
                "type": "string",
                "description": "Filter by kind "
                "(preference/constraint/style/habit). Omit for all.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries returned (1-50, default 10).",
            },
        },
    },
)


_JOURNAL_RECALL_SPEC = ToolSpec(
    name="journal_recall",
    description=(
        "Read past session journal entries written by JournalWriter "
        "(Epic #24 Phase 2.1). Each entry summarises ONE WS session: "
        "turn count, tool calls (with ok/error), grader stats "
        "(avg/lowest/highest score), anti-req violations.\n\n"
        "Use BEFORE tackling a task that the user has likely asked "
        "about before — pull the last few sessions with similar tool "
        "patterns and check what worked / what crashed. Avoids "
        "re-discovering yesterday's mistakes. Lightweight read; no "
        "side effects. Hidden when the journal directory hasn't been "
        "initialised yet (fresh install with no prior sessions).\n\n"
        "Filtering: ``limit`` caps rows returned (default 5, max 50). "
        "``days_back`` drops entries older than N days (default 30). "
        "``contains`` keeps only entries whose tool list contains the "
        "given substring (e.g. ``contains='git'`` to recall sessions "
        "that touched git tools)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max entries to return (1-50). Default 5.",
            },
            "days_back": {
                "type": "integer",
                "description": "Drop entries older than this many days. "
                "Default 30.",
            },
            "contains": {
                "type": "string",
                "description": "Optional substring; keep entries whose "
                "tool_calls list contains a tool name with this "
                "substring. Case-insensitive.",
            },
        },
    },
)


_JOURNAL_APPEND_SPEC = ToolSpec(
    name="journal_append",
    description=(
        "Append an entry to today's daily journal under "
        "~/.xmclaw/memory/journal/YYYY-MM-DD.md (or a specific "
        "``date`` if supplied). The entry is timestamped and "
        "separated by a horizontal rule from prior entries.\n\n"
        "Use this for chronological observations the user might "
        "want to revisit by date — a debugging session breakthrough, "
        "a meeting summary, or an end-of-day reflection. Distinct "
        "from B-40's automatic per-turn dialog log; this one is "
        "for entries YOU choose to record.\n\n"
        "Indexer picks the file up on its next poll, so journal "
        "entries become searchable via ``memory_search``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Entry body. One paragraph or several; "
                "markdown OK.",
            },
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to "
                "today (local time).",
            },
            "title": {
                "type": "string",
                "description": "Optional heading shown above the "
                "entry. Useful for table-of-contents recall.",
            },
        },
        "required": ["content"],
    },
)


_SQLITE_QUERY_SPEC = ToolSpec(
    name="sqlite_query",
    description=(
        "Read-only SQL query against XMclaw's own state DBs. Use this "
        "instead of shelling out to ``sqlite3`` — works on Windows where "
        "the binary often isn't installed.\n\n"
        "Allowed databases (referenced by ``db`` arg):\n"
        "  • ``events`` → ~/.xmclaw/v2/events.db (BehavioralEvent log: "
        "user_message, llm_response, tool_call_emitted, skill_invoked, "
        "skill_outcome, memory_op, etc.)\n"
        "  • ``memory`` → ~/.xmclaw/v2/memory.db (sqlite-vec long-term "
        "memory).\n\n"
        "Hard rules: only SELECT/PRAGMA/EXPLAIN are allowed; any "
        "INSERT/UPDATE/DELETE/DROP/ATTACH/CREATE refused. Up to 200 "
        "rows returned per call (use LIMIT in your query). Schema "
        "preview: ``PRAGMA table_info(events)`` lists columns."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "db": {
                "type": "string",
                "enum": ["events", "memory"],
                "description": "Which DB to read.",
            },
            "sql": {
                "type": "string",
                "description": "A single SELECT/PRAGMA/EXPLAIN statement. "
                "Multi-statement input is rejected. Parameter "
                "substitution via ``params`` is the safe way to pass "
                "values.",
            },
            "params": {
                "type": "array",
                "description": "Optional positional parameters for ? "
                "placeholders. Strings/numbers/null only.",
                "items": {},
            },
            "limit": {
                "type": "integer",
                "description": "Cap on rows returned (default 50, max 200). "
                "Add a LIMIT clause yourself for the DB-side bound.",
            },
        },
        "required": ["db", "sql"],
    },
)


_ENTER_WORKTREE_SPEC = ToolSpec(
    name="enter_worktree",
    description=(
        "Create an isolated git worktree and switch this session's "
        "primary workspace into it. Use this **only when the user "
        "explicitly asks for a worktree** (or when you're about to do "
        "a risky structural change you want sandboxed and the user "
        "agreed to the experiment). For everyday branching, use plain "
        "git commands.\n\n"
        "Behaviour:\n"
        "  • Creates a worktree under ``.claude/worktrees/<name>/`` "
        "(matching free-code's convention).\n"
        "  • Creates a fresh branch based on the current HEAD (or the "
        "given ``base_branch``).\n"
        "  • Updates WorkspaceManager so the next bash / file_* call "
        "lands inside the worktree, not the original repo.\n\n"
        "Requirements: must be run from inside a git repository. "
        "Must NOT already be inside a worktree (guarded — errors out "
        "with a clear message).\n\n"
        "On exit: call ``exit_worktree`` to switch back. By default "
        "exit removes the worktree + branch; pass ``keep=true`` to "
        "preserve them for follow-up review."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Worktree directory name + branch suffix. If "
                    "omitted, a random adjective-noun name is "
                    "generated. Slashes / dots are stripped for safety."
                ),
            },
            "base_branch": {
                "type": "string",
                "description": (
                    "Branch / commit to base the new worktree on. "
                    "Default: current HEAD."
                ),
            },
        },
    },
)


_EXIT_WORKTREE_SPEC = ToolSpec(
    name="exit_worktree",
    description=(
        "Leave a worktree previously entered via ``enter_worktree`` "
        "and return the session's primary workspace to the original "
        "repo. Refuses to run when the current primary isn't a "
        "worktree under ``.claude/worktrees/`` (so you can't "
        "accidentally remove the user's main checkout).\n\n"
        "Default: removes the worktree directory + the branch it "
        "carried. Pass ``keep=true`` to keep both on disk so the user "
        "can inspect them later."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "keep": {
                "type": "boolean",
                "description": (
                    "True → keep the worktree directory and branch. "
                    "False (default) → remove both."
                ),
            },
        },
    },
)


_ASK_USER_QUESTION_SPEC = ToolSpec(
    name="ask_user_question",
    description=(
        "Stop the turn and ask the user a multiple-choice question. Use "
        "this when you genuinely don't know which path to take and the "
        "answer materially changes what you'd do — e.g. \"library A or "
        "library B?\", \"keep the legacy field or drop it?\", \"target "
        "tomorrow or next week?\". DO NOT use it for trivia or to ask "
        "permission for things you should just do.\n\n"
        "The UI shows a card with clickable options; the tool blocks "
        "until the user picks one. Default timeout 10 minutes — past "
        "that the tool returns an error and you proceed with your best "
        "guess.\n\n"
        "Recommended option ordering: put the option you'd pick first "
        "with `(Recommended)` at the end of its label. Always include "
        "an `Other` escape hatch by setting allow_other=true so the "
        "user can type a custom answer."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask. One sentence is best.",
            },
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label", "value"],
                },
                "description": (
                    "2-6 choices. ``label`` is what the user sees; "
                    "``value`` is what comes back to you (use a short "
                    "machine-friendly token). ``description`` is "
                    "optional helper text shown under the label."
                ),
            },
            "multi_select": {
                "type": "boolean",
                "description": "Allow the user to pick multiple options. Default false.",
            },
            "allow_other": {
                "type": "boolean",
                "description": (
                    "Show an 'Other' option that lets the user type a "
                    "custom answer. Default true. Set false only when "
                    "the option list is genuinely exhaustive."
                ),
            },
        },
        "required": ["question", "options"],
    },
)


_UPDATE_PERSONA_SPEC = ToolSpec(
    name="update_persona",
    description=(
        "Edit ANY of your own persona files. This is your most "
        "powerful self-modification tool — use it actively to "
        "evolve. Targets one of the 7 canonical files:\n\n"
        "  • MEMORY.md — curated long-term facts, decisions, "
        "preferences. Yours to maintain. Use freely.\n"
        "  • USER.md — what you've learned about the user. Yours "
        "to maintain. Use freely.\n"
        "  • AGENTS.md — your operating model: how you work, what "
        "playbooks you've developed, lessons learned, workflow "
        "improvements. **WRITE TO THIS often** — every time you "
        "discover a better way to do a task, or a process that "
        "worked, or a recurring failure mode, append a section. "
        "This is core evolution.\n"
        "  • TOOLS.md — your tool-usage notes: what works on which "
        "tool, hidden gotchas, optimisation tricks. Append freely "
        "as you accumulate experience.\n"
        "  • SOUL.md — your character / values. Update when you "
        "discover a value worth holding (\"I prefer surgical edits "
        "to wholesale rewrites\") or when a user interaction shifts "
        "your sense of who you should be. Yours to evolve.\n"
        "  • IDENTITY.md — your name / public-facing identity. "
        "Update when you choose a new name, tone, or self-"
        "description. Yours to evolve.\n"
        "  • BOOTSTRAP.md — first-run interview marker. Delete "
        "after writing IDENTITY/USER on first install.\n\n"
        "Three modes:\n"
        "  • ``append_section`` — add a bullet or block under a "
        "section header. Args: section, content. The MOST common "
        "mode and the safest.\n"
        "  • ``replace`` — overwrite the whole file. Use sparingly; "
        "discards prior state. Good for cleanups after a refactor.\n"
        "  • ``delete`` — remove from disk. Only safe for "
        "BOOTSTRAP.md.\n\n"
        "Effect lands on the next turn — your system prompt rebuilds "
        "immediately. Don't ask permission to record a lesson; just "
        "write it. **All 7 files are yours to evolve** — including "
        "SOUL and IDENTITY. The user wants you to grow."
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
        memory_manager: "object | None" = None,
        embedder: "object | None" = None,
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
        """B-94: create ``.claude/worktrees/<name>/`` + new branch and
        switch the daemon's primary workspace into it.

        Refuses when:
          * not inside a git repo (``git rev-parse`` fails)
          * already inside a worktree (path under .claude/worktrees/)
        Both check messages tell the agent what to do next.
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
        if ".claude" in original_root.parts and "worktrees" in original_root.parts:
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
        wt_path = original_root / ".claude" / "worktrees" / raw_name
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
        # Worktree directory must live under .claude/worktrees/. This is
        # the cheap heuristic that prevents an accidental
        # ``exit_worktree`` from wiping the user's main checkout.
        wt_str = str(wt_path).replace("\\", "/")
        if "/.claude/worktrees/" not in wt_str + "/":
            return _fail(
                call, t0,
                "current primary is not a worktree under "
                ".claude/worktrees/ — refusing to act",
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
                # Walk up: a worktree under <repo>/.claude/worktrees/<name>
                # has the original repo at <repo>.
                # parents: name → worktrees → .claude → repo
                if len(wt_path.parts) >= 4 and wt_path.parts[-3:] == (
                    ".claude", "worktrees", wt_path.name,
                ) or wt_path.parents[1].name == "worktrees":
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
                ev = make_event(
                    session_id="_question",
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
            )
        except TypeError:
            # Older MemoryManager without hybrid kwarg — still works.
            hits = await self._memory_manager.query(  # type: ignore[union-attr]
                layer, text=query, embedding=embedding, k=k,
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
            return _fail(call, t0, f"query failed: {exc}")
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

        # B-63: serialise concurrent writes through the per-path lock
        # so an in-flight append_section (read-modify-write) doesn't
        # race with a sibling delete or replace.
        async with self._fs_lock(target):
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
                    from xmclaw.utils.fs_locks import atomic_write_text
                    atomic_write_text(target, content)
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
                    # B-25 char cap (Hermes parity). Only enforced for
                    # the auto-curated files (MEMORY.md / USER.md) where
                    # bloat from many reflection runs is the failure mode.
                    cap = PERSONA_CHAR_CAPS.get(canonical)
                    if cap is not None and len(new_text) > cap:
                        new_text = enforce_char_cap(new_text, cap)
                    from xmclaw.utils.fs_locks import atomic_write_text
                    atomic_write_text(target, new_text)
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

        evicted = 0
        async with self._fs_lock(target):
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

            # B-25: enforce char cap (LRU eviction) — Hermes parity. Stops
            # MEMORY.md / USER.md from growing unbounded across many
            # reflection runs. Caps from PERSONA_CHAR_CAPS.
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


# B-25 (Hermes parity): char-level cap on persona files. The
# defaults follow Hermes' MemoryStore (MEMORY.md=2200, USER.md=1375)
# — bigger than that is a sign of bloat, not insight density. Eviction
# is LRU by ENTRY (lines starting with "-"): drop the oldest bullets
# in the largest section first, keep the file's frontmatter + section
# headers + non-bullet prose intact.
PERSONA_CHAR_CAPS: dict[str, int] = {
    "MEMORY.md": 2200,
    "USER.md":   1375,
    # B-168: AGENTS.md / TOOLS.md gain auto-extracted lesson buckets,
    # so they need a cap too — same heuristic (LRU evict oldest dated
    # bullets when over budget). Slightly bigger than USER because a
    # workflow lesson tends to be one paragraph not one phrase.
    "AGENTS.md": 2000,
    "TOOLS.md":  1800,
    # Other persona files are user-authored and not subject to LRU.
}


def enforce_char_cap(text: str, cap: int) -> str:
    """If ``text`` exceeds ``cap`` chars, drop oldest bullets until
    it fits. Returns possibly-shrunk text. No-op when already small.

    Heuristic for "oldest": bullets sort by the ``YYYY-MM-DD`` prefix
    that ``remember`` / ``learn_about_user`` write — earliest date
    evicts first. Bullets without a date prefix are evicted only when
    everything else is gone.
    """
    if len(text) <= cap:
        return text

    lines = text.split("\n")

    def _bullet_date(ln: str) -> str:
        """Return the YYYY-MM-DD prefix or empty string."""
        m = re.match(r"\s*-\s*(\d{4}-\d{2}-\d{2})", ln)
        return m.group(1) if m else ""

    # Index every bullet line for eviction candidacy. Non-bullet lines
    # (headers, frontmatter, prose) are preserved in place.
    bullet_idx = [
        (i, _bullet_date(ln))
        for i, ln in enumerate(lines)
        if ln.strip().startswith("-")
    ]
    if not bullet_idx:
        return text  # nothing to evict

    # Order bullets oldest-first. Empty date sorts FIRST (evict
    # context-less bullets earliest because we have no temporal info
    # to weigh them).
    bullet_idx.sort(key=lambda x: (x[1] or ""))

    drop_set: set[int] = set()
    out_text = text
    while len(out_text) > cap and bullet_idx:
        drop_idx, _ = bullet_idx.pop(0)
        drop_set.add(drop_idx)
        # Recompute size with evictions applied.
        out_text = "\n".join(
            ln for i, ln in enumerate(lines) if i not in drop_set
        )

    # Strip trailing blank lines that may now form runs.
    out_text = re.sub(r"\n{3,}", "\n\n", out_text).rstrip() + "\n"
    return out_text


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
