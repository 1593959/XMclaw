"""ToolSpec constants — declarative wire-shape for every built-in tool.

Lifted out of ``builtin.py`` (B-324) to keep that module's class body
walkable. Pure data — no imports beyond ``ToolSpec``, no behaviour.
The handler class in ``builtin.py`` reads these by importing them
under their original ``_*_SPEC`` names so call sites and tests don't
shift.

Adding a new tool: drop a ``_FOO_SPEC = ToolSpec(...)`` here, then
add the matching ``_foo`` async handler + ``self._tools[_FOO_SPEC.name] = self._foo``
mount line in ``BuiltinTools``. The split is purely organisational.
"""
from __future__ import annotations

from xmclaw.core.ir import ToolSpec


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
        "provider in metadata so you can tell.\n\n"
        "B-197: pass ``kind`` to restrict by record type — "
        "``preference`` for user style/format/language facts, "
        "``lesson`` for learned failure modes, ``principle`` for "
        "explicit user decisions, ``procedure`` for skill metadata, "
        "``identity`` for stable user-told facts, ``file_chunk`` for "
        "persona file content, ``session_summary`` for cross-session "
        "history. Omit ``kind`` to search across everything (default)."
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
            "kind": {
                "type": "string",
                "enum": [
                    "preference", "lesson", "principle", "procedure",
                    "identity", "file_chunk", "session_summary",
                    "curriculum", "code_chunk",
                ],
                "description": "B-197/B-210: restrict to records of "
                "this kind. ``code_chunk`` searches indexed workspace "
                "source files (set via ``evolution.memory."
                "workspace_paths``). Omit to search across all kinds, "
                "but for code-specific questions pass ``code_chunk`` "
                "to focus recall and avoid persona-fact noise.",
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
        "  • Creates a worktree under ``.xmworktrees/<name>/`` "
        "(B-235: XMclaw-native namespace; older worktrees still living "
        "under ``.claude/worktrees/`` are accepted by ``exit_worktree`` "
        "for back-compat).\n"
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
        "repo. Refuses to run when the current primary isn't a worktree "
        "under ``.xmworktrees/`` (or legacy ``.claude/worktrees/`` from "
        "pre-B-235 sessions) — so you can't accidentally remove the "
        "user's main checkout.\n\n"
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


_PROPOSE_CURRICULUM_EDIT_SPEC = ToolSpec(
    name="propose_curriculum_edit",
    description=(
        "B-200 / Phase 5 — propose a change to your **learning rules** "
        "themselves (LEARNING.md). Distinct from ``update_persona``: "
        "that one is for direct memory writes (lessons / preferences) "
        "you can make on your own. THIS one is for changes to **how "
        "you learn / when you trust yourself** — meta-rules — and "
        "REQUIRES user approval before taking effect.\n\n"
        "Why a separate gate: LEARNING.md is read every turn into "
        "your system prompt. A bad rule self-amplifies (you'd start "
        "applying it before realising it was wrong). The user has to "
        "see the diff first.\n\n"
        "When to use:\n"
        "  • You catch yourself doing the same wrong thing twice and "
        "want to write a 'never do X' rule (e.g. 'never refuse without "
        "investigating, ref chat 17:51' — exactly the B-199 case).\n"
        "  • You discover a META-pattern: 'when user asks about Y, "
        "always try Z first'.\n"
        "  • An existing principle is misfiring and you want to soften "
        "it (e.g. promote threshold too aggressive).\n\n"
        "When NOT to use:\n"
        "  • Logging a one-off lesson — use ``update_persona`` "
        "(write directly to AGENTS.md / MEMORY.md).\n"
        "  • Recording a user preference — that's auto-extracted; or "
        "use ``learn_about_user``.\n\n"
        "After you call this, the proposal is queued; the user reviews "
        "via ``xmclaw curriculum list`` / ``approve`` / ``reject``. "
        "Approved proposals apply immediately + show up in your next "
        "system prompt. **Don't call this for trivial wording tweaks** "
        "— gate is for substantive rule changes."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "enum": ["LEARNING.md"],
                "description": "Which curriculum file to edit. v0 "
                "supports LEARNING.md only; future versions may open "
                "SOUL.md / IDENTITY.md.",
            },
            "operation": {
                "type": "string",
                "enum": ["add_principle"],
                "description": "v0 supports ``add_principle`` only — "
                "append a new bullet under an existing section. "
                "modify / remove will arrive when the diff parser is "
                "robust enough not to lose user edits.",
            },
            "section": {
                "type": "string",
                "description": "Section heading the bullet goes under "
                "(e.g. '怀疑自己', '记忆操作的纪律'). Match must "
                "be exact — copy from the LEARNING.md you read at "
                "turn start.",
            },
            "content": {
                "type": "string",
                "description": "The new bullet text (markdown; will be "
                "prefixed with '- ' if not already). Keep it tight: "
                "one principle per bullet, sub-points indented.",
            },
            "rationale": {
                "type": "string",
                "description": "REQUIRED — why this rule needs to "
                "exist. The user reads this when deciding whether to "
                "approve. 1-3 sentences. Lazy rationale = guaranteed "
                "rejection.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of session_ids / event "
                "references that motivated this rule. Anchors the "
                "proposal in real history (vs. theoretical).",
            },
        },
        "required": [
            "target_file", "operation", "section", "content", "rationale",
        ],
    },
)


_CURRICULUM_LIST_SPEC = ToolSpec(
    name="list_curriculum_proposals",
    description=(
        "B-200 — list the curriculum-edit proposals you've filed plus "
        "their status (pending / approved / rejected). Use to check "
        "whether the user has reviewed your past proposals before "
        "filing a new similar one. Returns the most recent 20."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "approved", "rejected", "all"],
                "description": "Filter by status. Default: pending.",
            },
        },
    },
)
