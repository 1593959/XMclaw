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


_MAX_WEB_BYTES = 200_000
_BASH_DEFAULT_TIMEOUT = 30.0
_BASH_MAX_OUTPUT = 100_000
_VALID_TODO_STATUSES = {"pending", "in_progress", "done"}


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
    ) -> None:
        self._allowed = (
            [Path(d).resolve() for d in allowed_dirs] if allowed_dirs else None
        )
        self._enable_bash = enable_bash
        self._enable_web = enable_web
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
