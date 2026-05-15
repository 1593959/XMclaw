from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail, _parse_ddg_html as _parse_ddg_html

_BASH_DEFAULT_TIMEOUT = 30.0
_BASH_MAX_OUTPUT = 100_000
_MAX_WEB_BYTES = 50_000

# SSRF blocklist — private, loopback, link-local, and well-known
# cloud metadata endpoints that should never be reachable via
# the web_fetch tool.
_SSRF_DISALLOWED_HOSTS = frozenset({
    "localhost", "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
})
_SSRF_DISALLOWED_PATTERNS = [
    re.compile(r"^169\.254\.169\.254$"),          # AWS / Azure / GCP metadata
    re.compile(r"^metadata\d*\.google\.internal$"),
    re.compile(r"^.*\.metadata\.google\.internal$"),
]


def _check_url_for_ssrf(url: str) -> str | None:
    """Return an error string if *url* points to a private / internal
    endpoint, otherwise ``None``.

    Checks raw IP addresses against private/link-local ranges and
    blocks known cloud-metadata hostnames.  Does **not** follow
    redirects — callers that need redirect safety must re-check
    each hop target.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        return f"invalid URL: {exc}"

    # Reject URLs that embed credentials or use non-standard ports
    # in ways that commonly bypass naive filters.
    if "@" in (parsed.netloc or ""):
        return "URLs containing credentials are not allowed"

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        return "missing hostname"

    # 1. Raw IPv4 / IPv6 check
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            return f"private / internal IP address: {hostname}"
        if addr.is_multicast or addr.is_unspecified:
            return f"non-routable IP address: {hostname}"
    except ValueError:
        pass  # not a raw IP — proceed to hostname checks

    # 2. Hostname blocklist
    if hostname in _SSRF_DISALLOWED_HOSTS:
        return f"disallowed host: {hostname}"
    for pat in _SSRF_DISALLOWED_PATTERNS:
        if pat.match(hostname):
            return f"disallowed host pattern: {hostname}"

    # 3. Prevent IPv4-address-like hostnames that slipped past
    # ip_address() because they include a port (e.g. 127.0.0.1:8080).
    # parsed.hostname strips the port, so we already checked the raw IP.

    return None

class BuiltinToolsShellMixin:
    """Shell and web tools: bash, web_fetch, web_search."""

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

        # Sanitize environment before spawning a shell.  Removes
        # dynamic-library injection vectors that could be used to
        # intercept system calls or hijack child processes.
        _clean_env = os.environ.copy()
        for _dangerous in (
            "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
            "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
        ):
            _clean_env.pop(_dangerous, None)

        # Wave 23 fix: keep child shells windowless on Windows so
        # users don't see a black cmd.exe / bash.exe blink per tool
        # call. No-op on POSIX.
        from xmclaw.utils.subprocess_hidden import hidden_subprocess_kwargs
        _hidden = hidden_subprocess_kwargs()

        def _run() -> tuple[int, bytes]:
            if shell_exe is not None and shell_args is not None:
                proc = subprocess.run(
                    [shell_exe, *shell_args],
                    shell=False, cwd=cwd,
                    capture_output=True, timeout=timeout,
                    env=_clean_env,
                    **_hidden,
                )
            else:
                proc = subprocess.run(
                    command, shell=True, cwd=cwd,
                    capture_output=True, timeout=timeout,
                    env=_clean_env,
                    **_hidden,
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
        # Wave-27 fix-14 (2026-05-16): include the LAST line of stderr +
        # an 8-char hash of the command in the error string. Pre-fix the
        # error was just ``command exited non-zero ({code})`` — identical
        # for ANY two failing bash calls regardless of what command was
        # run or what went wrong. hop_loop's stuck-loop detector uses
        # ``error[:80]`` as the signature, so 3 different curl probes
        # that all exit 1 with different stderr looked like "same error
        # 3x" → false-positive stuck-loop abort after 3 hops. Including
        # the command hash + last error line makes signatures actually
        # distinct when the underlying failure mode differs.
        if code == 0:
            err = None
        else:
            cmd_str = str(call.args.get("command", ""))
            cmd_hash = hashlib.sha1(
                cmd_str.encode("utf-8"),
            ).hexdigest()[:8] if cmd_str else "nohash"
            # Last non-empty line of merged output — usually the actual
            # error message ("curl: (6) Could not resolve host", "404
            # Not Found", etc.).
            tail_line = ""
            for raw_line in reversed(text.splitlines()):
                stripped = raw_line.strip()
                if stripped:
                    tail_line = stripped[:60]
                    break
            err = (
                f"command exited non-zero ({code}) "
                f"[cmd:{cmd_hash}] {tail_line}"
            )
        return ToolResult(
            call_id=call.id,
            ok=(code == 0),
            content=content,
            error=err,
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
        _ssrf_err = _check_url_for_ssrf(url)
        if _ssrf_err:
            return _fail(call, t0, f"SSRF protection: {_ssrf_err}")
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

