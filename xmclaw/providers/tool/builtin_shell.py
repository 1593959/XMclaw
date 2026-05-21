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


class _SearchBackendError(Exception):
    """Wave-27 fix-LAT8: raised when a search backend fails for a
    reason the caller can act on (missing API key, bad HTTP status,
    malformed response). Differs from generic ``Exception`` so the
    dispatcher in ``_web_search`` surfaces it as a clean tool error
    instead of a full traceback."""


class BuiltinToolsShellMixin:
    """Shell and web tools: bash, web_fetch, web_search."""

    # Wave-27 fix-LAT8: optional getter for the
    # ``evolution.search`` config block. Set by BuiltinTools.__init__
    # so the dispatched backend (bing/brave/google_cse/ddg) can look
    # up its API key without this layer reaching down into the config
    # singleton. None → default to DDG.
    _search_config_getter: Any | None = None

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

        # Wave-27 fix-LAT8 (2026-05-17): image content-type → vision
        # pipeline. Pre-fix, web_fetch on a PNG/JPG URL decoded the
        # bytes as text (``r.text``) and handed gibberish to the agent
        # — agent never SAW the image. The hop_loop already has a full
        # vision pipeline (metadata["attach_image"] → Message.images →
        # vision content block in the next LLM call) used by
        # browser_screenshot / screen_capture / image_view; web_fetch
        # was the one ingress that never joined it. Empirical fail:
        # ClawExam v3 returns image questions like
        # ``image: "/data/hle/hle_0.png"`` and Kimi-on-XMclaw scored
        # 0 on every visual question because of this miss.
        ct = (r.headers.get("content-type") or "").lower()
        ct_main = ct.split(";", 1)[0].strip()
        if ct_main.startswith("image/") and 200 <= r.status_code < 400:
            ext_map = {
                "image/png": ".png", "image/jpeg": ".jpg",
                "image/jpg": ".jpg", "image/gif": ".gif",
                "image/webp": ".webp", "image/bmp": ".bmp",
                "image/svg+xml": ".svg",
            }
            ext = ext_map.get(ct_main, ".bin")
            from xmclaw.utils.paths import data_dir as _data_dir
            cache_dir = _data_dir() / "web_fetch_cache"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            # Hash URL → stable filename so repeated fetches of the
            # same image dedupe to one file (saves disk + keeps the
            # vision pipeline pointed at a consistent path).
            h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            out = cache_dir / f"{h}{ext}"
            try:
                out.write_bytes(r.content)
            except OSError as exc:
                return _fail(
                    call, t0,
                    f"saved-bytes-write failed for {url}: {exc}",
                )
            content_summary = (
                f"[{r.status_code} {r.reason_phrase}] {url}\n"
                f"Content-Type: {ct}\n"
                f"Saved {len(r.content)} bytes → {out}\n"
                f"(image is attached to the next turn's vision input — "
                f"you can REFER to it directly; no need to OCR or re-fetch)"
            )
            return ToolResult(
                call_id=call.id,
                ok=True,
                content=content_summary,
                error=None,
                # B-VISION: hop_loop.py:1024 reads metadata["attach_image"]
                # and adds the file to the NEXT LLM call's
                # Message.images, which the anthropic/openai translators
                # encode as an image content block. So just setting
                # this key is enough — no other wiring needed.
                metadata={"attach_image": str(out)},
                side_effects=(str(out),),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        text = r.text
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        # B-273: scan fetched web content for prompt-injection before
        # it lands in the agent's context.  A compromised/malicious
        # webpage can embed invisible-unicode or instruction-override
        # payloads that the agent would otherwise execute.
        try:
            from xmclaw.security import (
                PolicyMode,
                SOURCE_WEB_FETCH,
                apply_policy,
            )
            decision = apply_policy(
                text,
                policy=PolicyMode.DETECT_ONLY,
                source=SOURCE_WEB_FETCH,
                extra={"url": url, "status_code": r.status_code},
            )
            if decision.blocked:
                return _fail(
                    call, t0,
                    "web_fetch blocked by prompt-injection policy",
                )
            text = decision.content
        except Exception:  # noqa: BLE001 — never block on scanner failure
            pass

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

    async def _open_in_user_browser(
        self, call: ToolCall, t0: float,
    ) -> ToolResult:
        """Open a URL in the user's foreground desktop browser.

        Wave-27 fix-LAT9: pre-fix, the agent had ONE browser path —
        headless Playwright (browser_open) — which the user can't see.
        For tasks like exam registration / dashboard inspection /
        anything that needs the user to LOOK at or INTERACT with a
        page, the agent had to dump the URL string in chat and hope
        the user clicked. Now there's a clean tool that calls
        ``webbrowser.open(url, new=2)`` — Python stdlib, routes through
        the OS's URL handler (which is the user's default browser
        with all their bookmarks / 2FA / extensions / saved logins
        intact).

        SSRF check is NOT applied here. The threat model is
        "user clicks a sketchy link the agent suggested", not "agent
        exfiltrates secrets via internal IP fetch". The user's
        browser is the audience, not me, so loopback /
        private-net URLs are legitimate (local dev servers,
        dashboards, etc.).
        """
        url = call.args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _fail(call, t0, "missing or empty 'url' argument")
        u = url.strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return _fail(
                call, t0,
                f"url must start with http(s)://, got {u!r}",
            )
        import webbrowser
        try:
            launched = webbrowser.open(u, new=2)  # new=2 = new tab
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"webbrowser.open failed ({type(exc).__name__}): {exc}",
            )
        if not launched:
            return _fail(
                call, t0,
                "webbrowser.open returned False — no default browser "
                "registered, or the URL handler failed. Tell the user "
                "to open the URL manually.",
            )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content={
                "url": u,
                "launched": True,
                "note": (
                    "Opened in user's default desktop browser. Tell "
                    "the user what they should do on that page — "
                    "they're looking at it now, not me."
                ),
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _web_search(self, call: ToolCall, t0: float) -> ToolResult:
        """Dispatch to the configured search backend.

        Wave-27 fix-LAT8: pre-fix this was hardcoded to DuckDuckGo's
        HTML endpoint. DDG's CJK relevance is poor (30-50% worse than
        Google for Chinese queries) and the HTML parser breaks
        whenever DDG ships a UI tweak. The agent gets stuck on a
        no-result for a query that would be trivial on a real search
        API. Backend is now picked from
        ``cfg.evolution.search.provider``:

          * ``ddg`` (default) — no key, HTML scrape, best-effort
          * ``bing`` — needs ``cfg.evolution.search.bing_api_key``
            (Azure Bing v7); returns structured JSON, much better
            for CJK
          * ``brave`` — needs ``cfg.evolution.search.brave_api_key``
            (Brave Web Search API); also no-cost-tier-friendly
          * ``google_cse`` — needs ``cfg.evolution.search.google_cse_id``
            + ``google_api_key``; best quality, paid

        Backends produce the same {title, url, snippet} shape; the
        caller never sees which engine ran.
        """
        query = call.args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _fail(call, t0, "missing or empty 'query' argument")
        max_results = call.args.get("max_results", 5)
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 20))

        # Resolve backend from the provider-injected config getter (or
        # fall back to ddg when no config is wired — tests, CLI mode).
        cfg = self._search_config_getter() if (
            getattr(self, "_search_config_getter", None) is not None
        ) else {}
        provider = (cfg.get("provider") or "ddg").lower()
        try:
            if provider == "bing":
                results = await self._search_bing(
                    query.strip(), max_results, cfg,
                )
            elif provider == "brave":
                results = await self._search_brave(
                    query.strip(), max_results, cfg,
                )
            elif provider == "google_cse":
                results = await self._search_google_cse(
                    query.strip(), max_results, cfg,
                )
            else:
                results = await self._search_ddg(
                    query.strip(), max_results,
                )
        except _SearchBackendError as exc:
            return _fail(call, t0, f"search backend error: {exc}")
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"search error ({type(exc).__name__}): {exc}",
            )

        if not results:
            return ToolResult(
                call_id=call.id, ok=True,
                content=(
                    f"(no results for {query!r} via {provider})"
                ),
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        blocks = [
            f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ]
        return ToolResult(
            call_id=call.id, ok=True,
            content=(
                f"{len(results)} results for {query!r} (via {provider}):"
                "\n\n" + "\n\n".join(blocks)
            ),
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _search_ddg(
        self, query: str, max_results: int,
    ) -> list[dict[str, str]]:
        import httpx
        url = "https://duckduckgo.com/html/"
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
        ) as c:
            r = await c.post(
                url, data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 XMclaw/2.x"},
            )
        if r.status_code != 200:
            raise _SearchBackendError(
                f"DDG returned HTTP {r.status_code}"
            )
        return _parse_ddg_html(r.text, max_results)

    async def _search_bing(
        self, query: str, max_results: int, cfg: dict,
    ) -> list[dict[str, str]]:
        key = cfg.get("bing_api_key")
        if not key:
            raise _SearchBackendError(
                "bing requires evolution.search.bing_api_key in config"
            )
        import httpx
        endpoint = cfg.get("bing_endpoint") or (
            "https://api.bing.microsoft.com/v7.0/search"
        )
        params = {
            "q": query, "count": max_results,
            "responseFilter": "Webpages",
            "textDecorations": "false", "textFormat": "Raw",
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                endpoint, params=params,
                headers={"Ocp-Apim-Subscription-Key": key},
            )
        if r.status_code != 200:
            raise _SearchBackendError(
                f"Bing returned HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        data = r.json()
        webpages = (data.get("webPages") or {}).get("value") or []
        return [
            {
                "title": (row.get("name") or "").strip(),
                "url": (row.get("url") or "").strip(),
                "snippet": (row.get("snippet") or "").strip(),
            }
            for row in webpages[:max_results]
        ]

    async def _search_brave(
        self, query: str, max_results: int, cfg: dict,
    ) -> list[dict[str, str]]:
        key = cfg.get("brave_api_key")
        if not key:
            raise _SearchBackendError(
                "brave requires evolution.search.brave_api_key in config"
            )
        import httpx
        endpoint = "https://api.search.brave.com/res/v1/web/search"
        params = {"q": query, "count": max_results}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                endpoint, params=params,
                headers={
                    "X-Subscription-Token": key,
                    "Accept": "application/json",
                },
            )
        if r.status_code != 200:
            raise _SearchBackendError(
                f"Brave returned HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        data = r.json()
        web = (data.get("web") or {}).get("results") or []
        return [
            {
                "title": (row.get("title") or "").strip(),
                "url": (row.get("url") or "").strip(),
                "snippet": (row.get("description") or "").strip(),
            }
            for row in web[:max_results]
        ]

    async def _search_google_cse(
        self, query: str, max_results: int, cfg: dict,
    ) -> list[dict[str, str]]:
        key = cfg.get("google_api_key")
        cse_id = cfg.get("google_cse_id")
        if not key or not cse_id:
            raise _SearchBackendError(
                "google_cse requires evolution.search.google_api_key "
                "+ google_cse_id in config"
            )
        import httpx
        endpoint = "https://www.googleapis.com/customsearch/v1"
        # Google CSE caps num at 10 per request.
        n = max(1, min(max_results, 10))
        params = {"key": key, "cx": cse_id, "q": query, "num": n}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(endpoint, params=params)
        if r.status_code != 200:
            raise _SearchBackendError(
                f"Google CSE returned HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        data = r.json()
        items = data.get("items") or []
        return [
            {
                "title": (row.get("title") or "").strip(),
                "url": (row.get("link") or "").strip(),
                "snippet": (row.get("snippet") or "").strip(),
            }
            for row in items[:max_results]
        ]

    # ── todos (per-session plan tracker) ───────────────────────────────

