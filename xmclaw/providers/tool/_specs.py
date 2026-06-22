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
    read_only=True,
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
        "Write UTF-8 text to a file, creating parent directories as "
        "needed. Overwrites existing files (undo cabinet snapshots "
        "pre-state so an accidental overwrite is reversible). Omit "
        "``content`` (or pass empty string) to scaffold an empty file.\n\n"
        "**Anti-pattern to avoid:** Do NOT write a single giant file "
        "(>10 KB) in one call for multi-page artifacts (PPT, reports, "
        "webpages). Instead build incrementally: create the scaffold, "
        "then append or patch page-by-page / section-by-section. This "
        "keeps each step small, retryable, and within the LLM token "
        "budget."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path."},
            "content": {
                "type": "string",
                "description": (
                    "Text to write. Optional — missing or null is "
                    "treated as empty string (creates an empty file)."
                ),
            },
        },
        "required": ["path"],
    },
)

_LIST_DIR_SPEC = ToolSpec(
    name="list_dir",
    read_only=True,
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
        "Use file_read first to grab the exact ``old_text``. Exact match is "
        "tried first; if that fails, a whitespace-tolerant match (ignoring "
        "trailing whitespace + line-ending style) is attempted, so minor "
        "indentation/CRLF drift still applies cleanly. Set ``replace_all`` "
        "on an edit to replace every occurrence instead of requiring uniqueness."
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
                            "description": "Text to find. Must occur exactly once (unless replace_all).",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text. May be empty to delete.",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence instead of requiring a unique match. Default false.",
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
    read_only=True,
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
    read_only=True,
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


_UNDO_LIST_SPEC = ToolSpec(
    name="undo_list",
    read_only=True,
    description=(
        "List recent destructive file operations that can be undone. "
        "Sprint 0 trust infrastructure: every ``file_write``, "
        "``apply_patch``, and ``file_delete`` is auto-recorded with a "
        "reverse op for ``UNDO_WINDOW_S`` (30 min default). Use this "
        "to see what's still reversible before calling ``undo_recent``. "
        "Returns id / action / path / age for each active record, "
        "newest first."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "within_s": {
                "type": "number",
                "description": (
                    "How far back to look (seconds). Default 60. "
                    "Cap at 30*60 = 1800."
                ),
            },
        },
    },
)


_UNDO_RECENT_SPEC = ToolSpec(
    name="undo_recent",
    read_only=True,
    description=(
        "Reverse recent destructive file operations. By default undoes "
        "every active record within the last 10 seconds (newest first) "
        "— intended for the 'agent just made a bad write, undo it' "
        "loop. Pass ``action_id`` to undo ONE specific record (safer "
        "when multiple unrelated mutations happened). Returns per-"
        "action result (applied, action, path, reverse_kind).\n\n"
        "Reverse semantics:\n"
        "  * file existed before action → restore from backup\n"
        "  * file did NOT exist (action created it) → delete\n"
        "After undo, the record is marked done and the backup is "
        "deleted (frees disk). Idempotent — undoing twice is a no-op."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "within_s": {
                "type": "number",
                "description": "Seconds back. Default 10.",
            },
            "action_id": {
                "type": "string",
                "description": (
                    "Undo ONE specific record by id (returned by "
                    "the original tool result or ``undo_list``). "
                    "Mutually exclusive with within_s."
                ),
            },
            "action_filter": {
                "type": "string",
                "enum": ["file_write", "file_delete", "apply_patch"],
                "description": (
                    "Only undo records matching this action name. "
                    "Useful when you want to keep a file_write but "
                    "undo a file_delete."
                ),
            },
        },
    },
)


def _bash_description() -> str:
    """Build the bash tool description with OS-aware shell guidance.

    Wave-27 fix-LAT3 (2026-05-16): the description was generic ("Run a
    shell command…") and the LLM defaulted to bash syntax on every
    platform. Empirical fail: on Windows the underlying shell is
    PowerShell (builtin_shell.py:_bash routes through pwsh/powershell);
    the LLM emitted ``mkdir -p ~/lt-automation/{content,monitor,...}``
    and PowerShell rejected the brace expansion + the ``-p`` flag,
    surfacing as ``MissingArgument`` from FullyQualifiedErrorId. The
    LLM had no way to know — the tool advertised itself as bash. Fix
    is to tell the model exactly which shell it's hitting + flag the
    syntaxes that DON'T translate.
    """
    import sys
    common = (
        "Run a shell command on the local machine and return combined "
        "stdout+stderr plus the exit code. Use for directory listings, "
        "finding files, git status, etc. Be careful with destructive "
        "commands — there is no undo.\n\n"
        "**Anti-pattern to avoid:** Do NOT generate a single massive "
        "script that tries to do everything (e.g. 'create a 20-slide PPT "
        "in one Python script'). Such scripts are error-prone, hard to "
        "debug, and often exceed the LLM output limit or stall. Instead "
        "break complex work into small, verifiable steps — use the "
        "file_write / apply_patch tools to build incrementally."
    )
    if sys.platform == "win32":
        return (
            common
            + "\n\n"
            + "⚠ HOST SHELL = **Windows PowerShell** (NOT bash). The tool "
            "is named ``bash`` for historical compatibility but the "
            "underlying interpreter is ``pwsh``/``powershell``. The "
            "following bash-isms FAIL on this host:\n"
            "  • Brace expansion ``{a,b,c}`` — PowerShell treats this "
            "as a script block. Use multiple ``New-Item`` calls or "
            "``'a','b','c' | ForEach-Object {{ New-Item -ItemType "
            "Directory -Path \"dir/$_\" }}`` instead.\n"
            "  • ``mkdir -p`` — PowerShell's ``mkdir`` is "
            "``New-Item -ItemType Directory`` and auto-creates parents "
            "by default; no ``-p`` flag.\n"
            "  • ``cmd && cmd2`` pipeline chaining — PowerShell 5.1 "
            "does NOT support ``&&``/``||``. Use ``cmd; if ($?) "
            "{{ cmd2 }}`` for conditional chaining, or ``;`` for "
            "unconditional.\n"
            "  • ``$VAR`` env var refs — in PowerShell use "
            "``$env:VAR``. Read with ``$env:PATH``; set with "
            "``$env:VAR='value'``.\n"
            "  • Heredocs ``<<EOF`` — use here-strings ``@'...'@`` "
            "(literal) or ``@\"...\"@`` (interpolated).\n"
            "  • Globs in unquoted commands behave differently — "
            "wrap paths with wildcards in quotes if you hit "
            "argument-parsing errors.\n"
            "POSIX aliases that DO work: ``ls``, ``cat``, ``pwd``, "
            "``rm``, ``cp``, ``mv``, ``echo``, ``grep`` (→ "
            "Select-String). Most ``git``/``npm``/``python`` "
            "invocations work identically. When you need a true bash "
            "feature, fall back to invoking ``python -c \"...\"`` "
            "or the ``code_python`` tool instead."
        )
    return (
        common
        + "\n\n"
        + "Host shell: POSIX bash (or sh on minimal images). Standard "
        "bash syntax including brace expansion, ``&&``/``||``, "
        "heredocs, and process substitution all work."
    )


_BASH_SPEC = ToolSpec(
    name="bash",
    description=_bash_description(),
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
    read_only=True,
    description=(
        "GET 请求 URL 并返回内容。自动跟随重定向。\n\n"
        "★ 内容类型自动检测：\n"
        "  • text/html / text/* / application/json → 返回文本（受 max_chars 限制）。\n"
        "  • image/* → 保存到本地缓存，下一条 LLM 提示自动以 vision 内容块"
        "附加图片，无需 OCR 或 base64。\n\n"
        "用户提到具体 URL 时立即调用，支持文本和图片 URL。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full http(s) URL."},
            "max_chars": {
                "type": "integer",
                "description": (
                    "Truncation cap for text content. Default 200000. "
                    "Ignored for images."
                ),
            },
        },
        "required": ["url"],
    },
)

_WEB_SEARCH_SPEC = ToolSpec(
    name="web_search",
    read_only=True,
    description=(
        "网络搜索。后端由配置决定：\n"
        "  • ddg（默认，无需 API key）— DuckDuckGo，英文尚可，CJK 一般。\n"
        "  • bing — Azure Bing，需 bing_api_key，CJK 效果更好。\n"
        "  • brave — Brave Search API，有免费额度。\n"
        "  • google_cse — Google 自定义搜索，质量最高，付费。\n\n"
        "返回 'TITLE\\nURL\\nSNIPPET' 格式结果，输出中标注所用引擎。"
        "需要查事实、找文档、确认信息时主动调用。"
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

_OPEN_IN_USER_BROWSER_SPEC = ToolSpec(
    name="open_in_user_browser",
    description=(
        "FIRE-AND-FORGET: open a URL in the user's desktop browser "
        "(Chrome/Edge/Firefox/etc). I CANNOT see or control the page "
        "after launching.\n\n"
        "Use ONLY when:\n"
        "  • I just want to hand the user a link to look at "
        "    themselves, and I DON'T need to operate the page.\n\n"
        "DO NOT use for login/QR-code/scraping — use browser_open "
        "instead (it opens a real window I control, so the user "
        "can scan/assist and I keep working in the same session)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full http(s):// URL.",
            },
        },
        "required": ["url"],
    },
)

_TODO_WRITE_SPEC = ToolSpec(
    name="todo_write",
    description=(
        "将当前多步任务的计划记录为 todo 列表。"
        "每项包含 content 和 status (pending|in_progress|done)。"
        "覆盖写入全列表；进度推进时再次调用并更新状态。"
        "用户在侧边栏看到实时 'Todos' 面板。\n\n"
        "## 何时使用\n"
        "  • 任务需要 3 个或以上独立步骤\n"
        "  • 任务复杂，需要跟踪进度（用户可能中途询问'做到哪了'）\n"
        "  • 用户明确要求使用 todo\n"
        "  • 用户一次给出多个任务（编号或逗号分隔）\n"
        "  • 计划模式（plan mode）中分解工作\n\n"
        "## 何时不使用\n"
        "  • 单一步骤的 straightforward 任务\n"
        "  • 任务 trivial，跟踪它没有任何组织收益\n"
        "  • 纯对话或信息查询（不需要执行步骤）\n\n"
        "NOTE: 只有 1 个 trivial 任务时，直接执行，不要写 todo。"
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
    read_only=True,
    description=(
        "Read back the current todo list for this session. Use this "
        "before updating statuses to make sure nothing was missed."
    ),
    parameters_schema={"type": "object", "properties": {}},
)

# Wave-27 fix-8 / C — update_focus: agent self-declares its current
# working focus for this session. Mirrors how Claude uses TodoWrite
# to externalise its working memory across turns. The recorded focus
# gets re-injected into every GoalAnchor block so the LLM sees its
# own most-recent intent at the top of every refresh — survives
# across compression / context shuffling because the anchor is
# regenerated each hop.
_UPDATE_FOCUS_SPEC = ToolSpec(
    name="update_focus",
    description=(
        "Record what you (the agent) are currently focused on in this "
        "session. Call this whenever your task understanding shifts — "
        "e.g. user moved from 'tune parameter X' to 're-architect Y', "
        "or you've completed milestone A and are starting on B. The "
        "recorded text gets injected into the goal-anchor every few "
        "hops so future-you doesn't lose track of the current phase "
        "across a long conversation.\n\n"
        "WHEN TO CALL:\n"
        "  • User redirects the task (new direction, scope change)\n"
        "  • Milestone done → moving to the next one\n"
        "  • Long debugging session → record what you're hunting for\n"
        "  • Multi-turn refactor → record which file / area you're in\n\n"
        "WHEN NOT TO CALL:\n"
        "  • Routine intra-task tool calls (use todo_write for those)\n"
        "  • Trivial answers / single-turn replies\n\n"
        "Empty / blank ``focus`` clears the slot (use when the task is "
        "complete and there is no active focus)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": (
                    "One short sentence describing your current focus. "
                    "Under 200 chars. Examples: '重新设计 token-budget "
                    "驱动的压缩,代替旧的消息条数硬编码', 'debugging the "
                    "websocket reconnect drop after daemon restart'."
                ),
            },
        },
        "required": ["focus"],
    },
)

_REMEMBER_SPEC = ToolSpec(
    name="remember",
    description=(
        "将跨会话持久事实写入 MEMORY.md。\n\n"
        "## 何时使用\n"
        "  • 用户明确要求'记住'某事\n"
        "  • 你做出了跨会话仍需的决策（项目约定、技术选型、架构方向）\n"
        "  • 发现了反复出现的约束或失败模式\n"
        "  • 用户纠正了你的行为，且该纠正适用于未来\n\n"
        "## 何时不使用\n"
        "  • 本会话临时上下文（用 todo_write）\n"
        "  • 用户个人信息如角色、偏好、沟通风格（用 learn_about_user）\n"
        "  • 一次性观察，下回合就不再相关\n"
        "  • 纯执行结果无需记忆（如'ls 返回了 5 个文件'）\n\n"
        "每次调用在匹配的 ## 分类下追加时间戳 bullet；分类不存在则自动创建。"
        "效果在下一回合立即生效——系统提示会实时重建。"
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
        "将用户个人信息写入 USER.md。\n\n"
        "## 何时使用\n"
        "  • 用户透露了身份相关信息（职业、公司、专业领域）\n"
        "  • 用户表达了明确的偏好（语言、格式、沟通风格、工具选择）\n"
        "  • 用户纠正了你的行为方式，且该纠正具有持久价值\n"
        "  • 用户提到了 recurring 的项目、团队或工作流程\n\n"
        "## 何时不使用\n"
        "  • 项目技术决策或代码约定（用 remember）\n"
        "  • 一次性请求或会话级变化（用 todo_write）\n"
        "  • 你已经通过 recall_user_preferences 确认该信息已存在\n\n"
        "效果下一回合生效。"
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
    read_only=True,
    description=(
        "搜索长期记忆。\n\n"
        "## 何时使用\n"
        "  • 用户问'我之前说过什么''我记得提到过'等涉及历史的问题\n"
        "  • 你需要确认用户的偏好、约束或过往决策再行动\n"
        "  • 用户要求基于之前的讨论继续工作\n"
        "  • **主动召回**：当你对用户/项目的某个事实**没把握**、或自动注入的"
        "``<memory-recall>`` 不够时，**先查一次记忆再回答**，不要凭空猜或反问"
        "用户已经说过的事——自动召回只给最相关的几条，更深的信息要你主动来取。\n"
        "  • 代码相关问题：先查 ``kind='code_chunk'`` 看工作区是否已有相关源码索引\n\n"
        "## 何时不使用\n"
        "  • 当前会话中刚刚发生的事实（还在上下文里，直接引用）\n"
        "  • 需要读取具体文件内容时（用 file_read，memory_search 只返回摘要）\n"
        "  • 结构/定量问题如'今天用了多少次工具'（用 sqlite_query）\n\n"
        "跨所有已连接后端合并结果（向量库优先，关键词补齐）。\n\n"
        "B-197: 用 ``kind`` 过滤记录类型："
        "``preference`` 用户偏好, ``lesson`` 经验教训, "
        "``principle`` 用户明确决策, ``procedure`` 技能元数据, "
        "``identity`` 用户身份事实, ``file_chunk`` 人格文件内容, "
        "``session_summary`` 跨会话历史, ``code_chunk`` 代码片段。"
        "省略 ``kind`` 则全库搜索（默认）。"
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


def _build_memory_spec_description() -> str:
    """2026-06-18: unified memory tool — all memory operations in one
    surface.  Backward-compatible with the 10 legacy single-purpose
    tools (memory_search, memory_compact, …) which are still wired
    but hidden from list_tools.
    """
    from xmclaw.memory.v2.buckets import render_for_prompt
    return (
        "**Unified** memory interface — every memory operation is a "
        "single call with an ``action`` parameter.\n\n"
        "  • ``search`` — semantic search across all wired providers.\n"
        "  • ``compact`` — trigger an immediate Auto-Dream pass.\n"
        "  • ``forget`` — soft-delete a fact (v3 path via old_fid / query).\n"
        "  • ``correct`` — replace a wrong fact with a corrected one.\n"
        "  • ``dedup`` — run semantic dedup on a scope/bucket.\n"
        "  • ``inspect`` — read-only health probe (fact counts, dup ratio).\n"
        "  • ``add``   — record a NEW fact.\n"
        "  • ``replace`` — supersede an old fact with a corrected one.\n"
        "  • ``pin``   — record a fact that must never be auto-deleted.\n"
        "  • ``get``   — read a persona MD file verbatim.\n"
        "  • ``graph_neighbors`` — walk the memory graph from a fact_id.\n"
        "  • ``multi_action`` — v3 multi-action alias; use ``sub_action`` "
        "to pick add/replace/forget/pin.\n\n"
        "★ Bucket selection (required for add / pin / replace):\n"
        f"{render_for_prompt()}\n\n"
        "When in doubt, use ``misc`` — facts there still land in "
        "MEMORY.md ## Other facts (recent) and are searchable.\n\n"
        "Replaces the legacy single-purpose tools (``memory_search`` / "
        "``memory_compact`` / ``memory_forget`` / ``memory_correct`` / "
        "``memory_dedup`` / ``memory_inspect`` / ``memory_get`` / "
        "``memory_graph_neighbors`` / ``memory_pin``). "
        "Those tool names still work for backward compat but new code "
        "should use ``memory`` to keep the choice tree simple."
    )


def _bucket_enum() -> list[str]:
    """All registered bucket tags — single source of truth for both
    the multi-action ``memory`` tool's JSON schema enum and any
    future introspection (UI dropdowns, doctor checks, etc.)."""
    from xmclaw.memory.v2.buckets import BUCKETS
    return list(BUCKETS.keys())


_MEMORY_SPEC = ToolSpec(
    name="memory",
    read_only=False,  # Fix audit 2026-06-11: add/replace/forget/pin are all writes
    description=_build_memory_spec_description(),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "search", "compact", "forget", "correct",
                    "dedup", "inspect", "add", "replace", "pin",
                    "get", "graph_neighbors", "multi_action",
                ],
            },
            "sub_action": {
                "type": "string",
                "enum": ["add", "replace", "forget", "pin"],
                "description": (
                    "When ``action='multi_action'``, use ``sub_action`` "
                    "to pick the v3 operation. Ignored for all other actions."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "For add/pin/replace: the fact text (one sentence). "
                    "For forget: ignored (use ``query`` instead)."
                ),
            },
            "bucket": {
                "type": "string",
                "enum": _bucket_enum(),
                "description": (
                    "Required for add/pin. For replace: where the NEW "
                    "fact should land (omit to inherit the old fact's "
                    "bucket). Ignored for forget."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["session", "user", "project", "global"],
                "description": "Default: 'user'.",
            },
            "kind": {
                "type": "string",
                "description": (
                    "preference / decision / identity / commitment / "
                    "correction / project / lesson / fact. Default is "
                    "derived from the bucket."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0. Default 0.85.",
            },
            "due_ts": {
                "type": "number",
                "description": (
                    "Unix timestamp. **Required** for "
                    "``bucket=commitment``. When set, a cron entry "
                    "fires at due_ts to surface a proactive notification "
                    "and auto-forget the commitment fact."
                ),
            },
            "old_fid": {
                "type": "string",
                "description": (
                    "For replace/forget: the EXACT fid of the fact to "
                    "supersede or remove. Pull from a recent "
                    "memory_search result, or from the "
                    "``<!-- fid:xxx -->`` markers in rendered .md files."
                ),
            },
            "old_text": {
                "type": "string",
                "description": (
                    "For replace: semantic search phrase to locate the "
                    "wrong fact. ``old_fid`` wins if both supplied."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "For search / forget: natural-language query or "
                    "semantic search phrase."
                ),
            },
            "max_matches": {
                "type": "integer",
                "description": (
                    "For forget: cap on how many top hits to remove. "
                    "Default 3, max 10."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional one-line audit note for replace/forget."
                ),
            },
            "k": {
                "type": "integer",
                "description": (
                    "For search: top-k hits per provider (default 5, max 20)."
                ),
            },
            "layer": {
                "type": "string",
                "enum": ["short", "working", "long"],
                "description": "For search: memory layer (default 'long').",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "For search: total result chars cap (default 6000, "
                    "min 500, max 20000)."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["vector", "llm"],
                "description": (
                    "For dedup: 'vector' (default, fast cosine) or 'llm' "
                    "(semantic, catches paraphrases)."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "For dedup: when true (default), returns the preview "
                    "without superseding anything. Pass false to commit."
                ),
            },
            "sample_dup_check": {
                "type": "integer",
                "minimum": 50,
                "maximum": 2000,
                "description": (
                    "For inspect: how many facts to sample when estimating "
                    "duplicate ratio. Default 500."
                ),
            },
            "file": {
                "type": "string",
                "description": "For get: persona MD basename (case-insensitive).",
            },
            "section": {
                "type": "string",
                "description": (
                    "For get: optional ``## Section header`` to extract just "
                    "that segment. ``## `` prefix is optional."
                ),
            },
            "lines": {
                "type": "string",
                "description": (
                    "For get: optional line range like '10-50' (1-indexed, "
                    "inclusive). Applied AFTER section filtering."
                ),
            },
            "fact_id": {
                "type": "string",
                "description": (
                    "For graph_neighbors: starting fact id, e.g. 'fid:abc123'."
                ),
            },
            "relation_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For graph_neighbors: optional filter. "
                    "E.g. [\"SAME_TOPIC\", \"SUPERSEDES\"]."
                ),
            },
            "max_hops": {
                "type": "integer",
                "description": (
                    "For graph_neighbors: graph hops to traverse (1-3, default 1)."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "For legacy pin: the fact, one sentence. "
                    "Will be prefixed with the current date."
                ),
            },
            "new_text": {
                "type": "string",
                "description": (
                    "For correct: the correct value, as a complete fact sentence."
                ),
            },
        },
        "required": ["action"],
    },
)


_MEMORY_GET_SPEC = ToolSpec(
    name="memory_get",
    read_only=True,
    description=(
        "**Read a persona MD file (or a section of one) verbatim** "
        "from disk. Use when you need full structure / context that "
        "``memory_search``'s top-K snippets won't give you — for "
        "example after seeing a MEMORY.md ## Other facts (recent) "
        "entry referenced as ``<!-- fid:abc -->`` and wanting the "
        "surrounding bullets in the same section.\n\n"
        "Memory v3 file roles (all under ``~/.xmclaw/persona/"
        "profiles/<profile>/``):\n"
        "  • IDENTITY.md   — agent's own identity\n"
        "  • USER.md       — user identity + preferences\n"
        "  • AGENTS.md     — workflows\n"
        "  • TOOLS.md      — tool quirks\n"
        "  • SOUL.md       — agent values\n"
        "  • LEARNING.md   — hard rules\n"
        "  • MEMORY.md     — failure modes + project facts + "
        "commitments + other recent facts\n\n"
        "Output preserves ``<!-- fid:xxx -->`` markers so you can "
        "use those fids in subsequent ``memory(action=replace/forget)`` "
        "calls."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Persona MD basename (case-insensitive).",
            },
            "section": {
                "type": "string",
                "description": (
                    "Optional ``## Section header`` to extract just "
                    "that segment. ``## `` prefix is optional."
                ),
            },
            "lines": {
                "type": "string",
                "description": (
                    "Optional line range like '10-50' (1-indexed, "
                    "inclusive). Applied AFTER section filtering."
                ),
            },
        },
        "required": ["file"],
    },
)

_MEMORY_GRAPH_NEIGHBORS_SPEC = ToolSpec(
    name="memory_graph_neighbors",
    read_only=True,
    description=(
        "Walk the memory graph starting from a known fact_id to discover "
        "related facts via semantic relationships.\n\n"
        "**When to use**\n"
        "  • After ``memory_search`` returns a key fact and you suspect "
        "there are related facts (same topic, newer version, or "
        "contradictions) that weren't in the top-K results.\n"
        "  • When you see a ``<!-- fid:xxx -->`` marker and want to "
        "explore what else is connected to that fact.\n"
        "  • When resolving contradictions: a fact may have a "
        "``SUPERSEDES`` or ``CONTRADICTS`` edge pointing to another "
        "fact you need to see.\n\n"
        "**Parameters**\n"
        "  • ``fact_id`` — the starting fact id (e.g. ``fid:abc123``).\n"
        "  • ``relation_types`` — filter by edge type. Common values: "
        "``SAME_TOPIC``, ``SUPERSEDES``, ``CONTRADICTS``, ``CAUSED_BY``. "
        "Omit for all relations.\n"
        "  • ``max_hops`` — how many graph hops to traverse (1-3, default 1). "
        "Higher hops find more context but cost more.\n\n"
        "Returns a list of ``{relation, target_fact_id, strength, text_preview}`` "
        "so you can decide which targets to fetch with ``memory_get`` or "
        "``memory_search``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "string",
                "description": "Starting fact id, e.g. 'fid:abc123'.",
            },
            "relation_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional filter. E.g. [\"SAME_TOPIC\", \"SUPERSEDES\"]."
                ),
            },
            "max_hops": {
                "type": "integer",
                "description": "Graph hops to traverse (1-3, default 1).",
            },
        },
        "required": ["fact_id"],
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


# 2026-05-26: memory curation tools (chat-b3c614bc follow-up).
# Pre-fix the agent's only memory mutators were ``remember`` /
# ``memory_pin`` (append-only) and ``update_persona`` (append /
# overwrite-whole-file / delete-file). When the user corrected a
# fact ("I'm not 张伟"), the agent could only APPEND a contradiction
# ("user is not 张伟"), leaving the original lie + the contradiction
# both visible to future system prompts. The 3 tools below close
# that gap. Backed by MemoryService.forget/correct/dedup_scope.

_MEMORY_FORGET_SPEC = ToolSpec(
    name="memory_forget",
    description=(
        "Soft-delete a fact (or facts) from L1 memory + persona "
        "files. Use this when the user tells you a previously "
        "captured fact is WRONG with no replacement value (\"我不是"
        "张伟\", \"forget that\", \"I never said that\"), OR when "
        "you're cleaning up demonstrably stale data.\n\n"
        "How it works: searches the memory store with ``query`` and "
        "marks up to ``max_matches`` best matches as forgotten. "
        "Forgotten facts are skipped by recall + persona render but "
        "remain on disk under a tombstone — recoverable via the "
        "admin path if needed.\n\n"
        "Distinct from ``memory_correct``: ``forget`` removes; "
        "``correct`` REPLACES. Pick ``correct`` when the user "
        "supplies the right value, ``forget`` when they don't."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search phrase to locate the bad fact(s) — uses "
                    "the same semantic search as memory_search. "
                    "Pass a short, specific phrase ('user name 张伟', "
                    "'works at LT凌天电竞') not whole sentences."
                ),
            },
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": (
                    "Cap on how many top hits to forget. Default 3. "
                    "Raise when you know multiple paraphrases of the "
                    "same wrong fact exist (typical after a long "
                    "test-conversation where one error got "
                    "re-extracted under different wording)."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional one-line explanation for the audit log "
                    "(e.g. 'user clarified they're not 张伟'). Not "
                    "shown to the model on future turns."
                ),
            },
        },
        "required": ["query"],
    },
)


_MEMORY_CORRECT_SPEC = ToolSpec(
    name="memory_correct",
    description=(
        "Replace a previously-captured fact with the corrected "
        "value. Use this whenever the user says \"actually X, not "
        "Y\" / \"I'm not Y, I'm X\" / \"that was wrong, it's X\".\n\n"
        "Finds the closest matching old fact via semantic search, "
        "writes a NEW fact carrying ``new_text`` (high confidence "
        "0.9 since the user just asserted it), and links the two "
        "via a SUPERSEDES edge. The renderer drops the old one, "
        "the new one ships in the next system prompt.\n\n"
        "If no fact crosses the similarity floor, the new fact is "
        "still written (so the corrected value is captured) but "
        "nothing is superseded — caller sees ``matched=false`` in "
        "the result. Do NOT use ``memory_correct`` to introduce "
        "fresh facts unrelated to anything in memory — that's what "
        "``remember`` is for."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "old_text": {
                "type": "string",
                "description": (
                    "The wrong claim to find + replace. Doesn't have "
                    "to be the verbatim stored text — semantic "
                    "search is fine. 'user is named 张伟' will match "
                    "'user profile loaded: 张伟 / LT凌天电竞'."
                ),
            },
            "new_text": {
                "type": "string",
                "description": (
                    "The correct value, as a complete fact sentence "
                    "(not just the delta). 'user is named 何鹏', not "
                    "just '何鹏'."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Optional fact-kind filter for the search "
                    "(preference / lesson / identity / etc.). When "
                    "omitted, search spans all kinds."
                ),
            },
            "scope": {
                "type": "string",
                "description": (
                    "Optional scope filter (user / project / "
                    "session). When omitted, search spans all "
                    "scopes."
                ),
            },
        },
        "required": ["old_text", "new_text"],
    },
)


_MEMORY_INSPECT_SPEC = ToolSpec(
    name="memory_inspect",
    read_only=True,
    description=(
        "Inspect the LanceDB fact store's health: total fact count, "
        "breakdown by (scope, kind), suspected near-duplicate ratio "
        "per scope, and oldest / largest entries. **Call this when "
        "you suspect memory bloat** — e.g., before / after a long "
        "session, before deciding whether to run memory_dedup / "
        "memory_forget.\n\n"
        "★ Self-grooming workflow (you can run this without being "
        "asked):\n"
        "  1. ``memory_inspect`` — see the picture\n"
        "  2. if ``dup_ratio > 0.15`` in some scope → "
        "``memory_dedup(scope=..., dry_run=true)`` to preview\n"
        "  3. if preview looks right → ``memory_dedup(scope=..., "
        "dry_run=false)`` to commit\n"
        "  4. if specific facts are demonstrably stale → "
        "``memory_forget(query=...)``\n\n"
        "Read-only — never modifies the store. Cheap; safe to call "
        "often."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": (
                    "Restrict inspection to one scope (user / "
                    "project / session). Omit for global picture."
                ),
            },
            "sample_dup_check": {
                "type": "integer",
                "minimum": 50,
                "maximum": 2000,
                "description": (
                    "How many facts to sample when estimating "
                    "duplicate ratio. Higher = more accurate, "
                    "slower. Default 500."
                ),
            },
        },
    },
)


_MEMORY_DEDUP_SPEC = ToolSpec(
    name="memory_dedup",
    description=(
        "Run semantic dedup on one bucket of memory (e.g. all "
        "user-scoped preferences) and merge near-duplicate facts. "
        "Use when you notice the persona file has many bullets "
        "saying basically the same thing — usually after a long "
        "session where ProfileExtractor captured the same insight "
        "under 5 different phrasings.\n\n"
        "Per cluster: keeps the highest-confidence + most-evidence "
        "+ newest survivor; supersedes the rest. Returns the "
        "merge groups so you can describe to the user what was "
        "consolidated.\n\n"
        "Default is ``dry_run=true`` — surfaces the preview "
        "without writing. Call again with ``dry_run=false`` once "
        "you've reviewed.\n\n"
        "★ ``mode`` (2026-05-29):\n"
        "  • ``vector`` (default) — fast embedding-cosine clustering "
        "(≥0.86). Catches near-identical re-phrasings.\n"
        "  • ``llm`` — **semantic** dedup: asks an LLM to cluster "
        "entries that MEAN the same thing even when worded very "
        "differently. Use this when you see the same rule/insight "
        "stored in 7-8 different phrasings that vector clustering "
        "left behind (e.g. \"空消息超3轮停止分析\" / \"连续3次空消息后"
        "中止\" / \"若3轮都空则停\"). Slower (one LLM call per ~60 "
        "facts) but catches what cosine can't."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["vector", "llm"],
                "description": (
                    "'vector' (default, fast cosine) or 'llm' "
                    "(semantic, catches paraphrases)."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Optional kind filter (preference / lesson / "
                    "identity / etc.). Omit to dedup across all "
                    "kinds within the scope."
                ),
            },
            "scope": {
                "type": "string",
                "description": (
                    "Optional scope filter (user / project / "
                    "session). Default is no scope filter — but "
                    "most useful runs target one scope at a time."
                ),
            },
            "bucket": {
                "type": "string",
                "description": (
                    "Optional persona-renderer bucket "
                    "('user_preference' / 'workflow' / 'values' / "
                    "etc.). Empty = no bucket filter."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "When true (default), returns the preview "
                    "without superseding anything. Pass false to "
                    "commit."
                ),
            },
        },
    },
)


_AGENT_STATUS_SPEC = ToolSpec(
    name="agent_status",
    read_only=True,
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
    read_only=True,
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
    read_only=True,
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
        "this when:\n"
        "  1. You genuinely don't know which path to take and the answer "
        "materially changes what you'd do — e.g. \"library A or "
        "library B?\", \"keep the legacy field or drop it?\", \"target "
        "tomorrow or next week?\".\n"
        "  2. The user's request involves ≥2 UNSTATED key parameters that "
        "would dramatically change the output (audience, format, depth, "
        "style, platform). NEVER guess and start building — pause and "
        "confirm with this tool first.\n\n"
        "★★ MUST USE when presenting multiple mutually-exclusive options "
        "to the user — e.g. \"how would you like to receive this file?\" "
        "(download directly / upload to cloud / email / other). The UI "
        "renders these as clickable cards. NEVER list options in plain "
        "text when ask_user_question is available — the card UI is clearer "
        "and the user's choice is unambiguous.\n\n"
        "DO NOT use it for trivia or to ask permission for things you "
        "should just do.\n\n"
        "The UI shows a card with clickable options; the tool blocks "
        "until the user picks one.\n\n"
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

_SEND_MEDIA_SPEC = ToolSpec(
    name="send_media",
    description=(
        "Send a GENERATED media file (video, audio, or programmatically-"
        "created image) to the user so they can view or download it "
        "directly in the chat. Use this after creating media with tools "
        "like ffmpeg, PIL, matplotlib, or an external script — e.g. "
        "rendering an MP4 video, synthesising audio, or compositing an "
        "image from HTML.\n\n"
        "★ DO NOT use send_media for screenshots. The screenshot / "
        "screen_capture / screen_region_capture tools already display "
        "the captured image inline in the chat — calling send_media "
        "with the same file will show a DUPLICATE. Screenshot results "
        "are auto-attached to your next prompt as vision blocks.\n\n"
        "★ DO NOT use send_media for text files or code — the user "
        "can read those via file_read.\n\n"
        "The file is copied to the chat's media directory and served "
        "via a secure URL. The UI renders images as thumbnails, videos "
        "as playable <video> elements, and audio as <audio> controls."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute filesystem path to the media file. "
                    "Examples: /home/user/Desktop/intro.mp4, "
                    "C:\\Users\\Alice\\Desktop\\recording.wav"
                ),
            },
        },
        "required": ["path"],
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
        "Four modes:\n"
        "  • ``append_section`` — add a bullet or block under a "
        "section header. Args: section, content. The MOST common "
        "mode and the safest.\n"
        "  • ``remove_bullet`` — surgically drop one or more list "
        "bullets from the file. Args: match (case-sensitive "
        "substring; every bullet whose line contains it is removed). "
        "Use this to delete a single wrong bullet without nuking "
        "the rest of the file. NOTE: only touches the manual / "
        "user-curated portion. Auto-extracted bullets live in "
        "LanceDB — use ``memory_forget`` for those (and the next "
        "render will drop them from the MD).\n"
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
                "enum": [
                    "append_section", "remove_bullet", "replace", "delete",
                ],
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
                "Ignored for delete / remove_bullet.",
            },
            "match": {
                "type": "string",
                "description": "Required for remove_bullet mode. "
                "Case-sensitive substring of the bullet line(s) you "
                "want removed. Every list bullet (- / *) whose line "
                "contains this substring is dropped. Use a unique "
                "snippet of the bullet text — be more specific than "
                "less, to avoid accidentally dropping siblings.",
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


# B-388 (Sprint 2): voice tools. Advertised conditionally by
# BuiltinTools.list_tools() based on whether stt_provider / tts_provider
# are wired. The agent uses these for transcribing user-supplied audio
# clips and producing TTS replies that channel adapters can attach to
# their outbound messages.
_VOICE_TRANSCRIBE_SPEC = ToolSpec(
    name="voice_transcribe",
    description=(
        "Transcribe an audio clip to text using the configured STT "
        "provider (default: faster-whisper local). Pass exactly ONE of "
        "``audio_path`` (filesystem path to .wav/.mp3/.m4a/.ogg/etc) or "
        "``audio_b64`` (base64-encoded audio bytes — useful when the "
        "channel adapter handed you bytes inline). Returns JSON: "
        "``{text: <recognized>, audio_bytes: <int>, source: <which arg>}``. "
        "Errors when no STT provider is configured (operator: pip install "
        "'xmclaw[voice-stt]' + set ``voice.stt`` in config)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "audio_path": {
                "type": "string",
                "description": "Filesystem path to the audio file.",
            },
            "audio_b64": {
                "type": "string",
                "description": "Base64-encoded audio bytes.",
            },
        },
    },
)


_VOICE_SYNTHESIZE_SPEC = ToolSpec(
    name="voice_synthesize",
    description=(
        "Synthesize the given text to speech using the configured TTS "
        "provider (default: Microsoft Edge free TTS, zh-CN-XiaoxiaoNeural). "
        "Writes the resulting mp3 to ``$XMC_DATA_DIR/v2/audio/<uuid>.mp3`` "
        "and returns its path. Returns JSON: ``{audio_path: <path>, "
        "bytes: <int>}``. Errors when no TTS provider is configured "
        "(operator: pip install 'xmclaw[voice-tts]' + set ``voice.tts`` "
        "in config)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to synthesize. Required.",
            },
            "voice": {
                "type": "string",
                "description": (
                    "Optional voice id. Provider-specific — for edge-tts "
                    "use values like ``zh-CN-XiaoxiaoNeural`` or "
                    "``en-US-AriaNeural``. Default: ``default`` (provider "
                    "picks)."
                ),
            },
        },
        "required": ["text"],
    },
)


# Wave-32+ (2026-05-18): plan-mode tools — ports the free-code
# EnterPlanMode / ExitPlanMode pattern. Plan mode is a SESSION-LEVEL
# state that lets the agent explicitly switch into "explore + design,
# don't write yet" before tackling non-trivial implementation work.
# While active, mutating tools (file_write, apply_patch, file_delete,
# bash) refuse cleanly with a "we're in plan mode" message.

_ENTER_PLAN_MODE_SPEC = ToolSpec(
    name="enter_plan_mode",
    description=(
        "Switch this session into PLAN MODE before tackling a "
        "non-trivial implementation. While active, read-only tools "
        "(file_read, glob_files, grep_files, web_search, etc.) keep "
        "working; mutating tools (file_write, apply_patch, "
        "file_delete, bash) REFUSE with a clear 'plan mode active' "
        "message. Use ``exit_plan_mode`` once you have a concrete "
        "plan to present.\n\n"
        "When to use:\n"
        "  • Multi-file refactors where the wrong approach wastes "
        "    significant effort.\n"
        "  • Architectural choices (auth scheme, state mgmt, cache "
        "    backend) where the user should approve direction.\n"
        "  • Tasks with unclear requirements that need codebase "
        "    exploration first.\n"
        "  • Anytime you'd otherwise call ``ask_user_question`` "
        "    several times in a row to clarify approach — plan "
        "    mode is a cleaner container for that flow.\n\n"
        "When NOT to use:\n"
        "  • Single-line typo / one-file bugfixes.\n"
        "  • Read-only research questions.\n"
        "  • The user said \"just do X\" — that's a directive, not "
        "    an invitation to plan."
    ),
    parameters_schema={
        "type": "object",
        "properties": {},
    },
)


_SET_OUTPUT_STYLE_SPEC = ToolSpec(
    name="set_output_style",
    description=(
        "Switch the conversation's OUTPUT STYLE — the tone and "
        "behavior preset the agent uses when explaining and "
        "delivering work. Persists for the session until called "
        "again with a different style or ``'default'``.\n\n"
        "Built-in styles:\n"
        "  • ``default`` — base behavior, no extra style fragment.\n"
        "  • ``Explanatory`` — adds short ★ Insight boxes around "
        "    code changes so the user understands *why* the agent "
        "    made each choice.\n"
        "  • ``Learning`` — agent inserts TODO(human) stubs and "
        "    explicitly asks the user to fill them in, optimizing "
        "    for hands-on practice.\n\n"
        "Operators can add custom styles by dropping markdown files "
        "into ``~/.xmclaw/output_styles/<name>.md`` (each file's "
        "name becomes the style name, contents become the style "
        "prompt).\n\n"
        "Use this proactively if the user signals what they want — "
        "e.g. \"explain as you go\" → Explanatory; \"teach me, let "
        "me do parts\" → Learning."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Style name. Use ``default`` to clear; use one "
                    "of the built-ins or a custom style file's name."
                ),
            },
        },
        "required": ["name"],
    },
)


_READ_CONVERSATION_HISTORY_SPEC = ToolSpec(
    name="read_conversation_history",
    read_only=True,
    description=(
        "Browse the current session's conversation history "
        "chronologically. Use this when the user refers to "
        "something said earlier (\"like I mentioned before…\", "
        "\"going back to what we discussed…\") and the reference "
        "is NOT in the active context window.\n\n"
        "Returns a slice of past messages with role and a short "
        "content preview. The assistant can then call this again "
        "with a different offset to page through history like a "
        "chat log.\n\n"
        "Directions:\n"
        "  • ``newest`` — start from the most recent message and "
        "    walk backward (default).\n"
        "  • ``oldest`` — start from the first user message and "
        "    walk forward.\n\n"
        "Tip: if the user says \"what did I ask you to do at the "
        "start?\" use direction=\"oldest\" with limit=5."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "offset": {
                "type": "integer",
                "description": (
                    "Number of messages to skip from the start of "
                    "the chosen direction. Default 0."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Max messages to return (1-50). Default 10."
                ),
            },
            "direction": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": (
                    "'newest' = recent-first (default); "
                    "'oldest' = chronological from start."
                ),
            },
        },
    },
)

_EXIT_PLAN_MODE_SPEC = ToolSpec(
    name="exit_plan_mode",
    description=(
        "Leave plan mode and present the implementation plan you "
        "drafted while in it. Mutating tools become available "
        "again after this call (subject to normal permission "
        "checks). The ``plan`` you submit is shown to the user "
        "verbatim — write it as the user will read it, with "
        "markdown structure and concrete file paths.\n\n"
        "If you discovered the task is simpler than expected and "
        "no real plan is needed, call this anyway with a one-line "
        "plan — that's the right way to leave plan mode cleanly."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "Markdown-formatted implementation plan. Should "
                    "include: (1) summary of what you'll do, (2) "
                    "ordered list of concrete steps, (3) files that "
                    "will be touched, (4) anything the user should "
                    "approve before you start."
                ),
            },
        },
        "required": ["plan"],
    },
)

_THINK_SPEC = ToolSpec(
    name="think",
    read_only=True,
    description=(
        "Use this tool to record your internal reasoning, analysis, or plan. "
        "The thought is saved to the session log but is NOT shown to the user. "
        "Use it when you need to work through complex logic before taking action, "
        "or when analyzing a tool result before deciding the next step. "
        "NEVER output reasoning as plain text in your response — always use this tool."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "thought": {
                "type": "string",
                "description": "Your internal thought process, reasoning, or analysis.",
            }
        },
        "required": ["thought"],
    },
)
