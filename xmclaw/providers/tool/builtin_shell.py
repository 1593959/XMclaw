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
from xmclaw.providers.tool._helpers import (
    _fail as _fail,
    _parse_bing_html as _parse_bing_html,
    _parse_ddg_html as _parse_ddg_html,
)

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
        # 2026-05-26 (audit F3 Layer 1): pre-flight the command against
        # the guardrail patterns BEFORE spawning a subprocess.
        # ``deny`` short-circuits with a refusal; ``confirm`` is also
        # treated as a refusal at this layer with a guidance message
        # pointing the LLM at ``ask_user_question`` — wiring it
        # through to an actual user confirmation is the follow-up.
        from xmclaw.providers.tool.bash_guardrails import classify_command
        _verdict = classify_command(command)
        if _verdict.decision == "deny":
            return _fail(
                call, t0,
                f"[bash_guardrail/{_verdict.pattern_id}] {_verdict.reason}",
            )
        if _verdict.decision == "confirm":
            return _fail(
                call, t0,
                f"[bash_guardrail/{_verdict.pattern_id}] {_verdict.reason} "
                f"Call ask_user_question first to get a YES/NO from "
                f"the user, then proceed if they approve.",
            )
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

        import asyncio as _asyncio
        import httpx
        # 2026-05-28: public sites reject the bot-looking
        # "XMclaw/2.x" UA with 403/empty, AND fail TLS / handshake
        # without proper Accept-* headers. Use a real Chrome 145 UA
        # plus the headers a modern browser actually sends so
        # Cloudflare / Akamai / Datadome don't trip on day one.
        # The ``Sec-*`` quartet is what real Chrome ships; without
        # them many anti-bot middleboxes return 403 for "missing
        # client hints".
        _BROWSER_HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Chromium";v="145", "Not?A_Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        # Split timeout: 10s to establish TCP+TLS, 30s for the body —
        # slow CN sites that take 25s to first byte but stream fast
        # used to die on a flat 15s.
        _timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        # Up to 3 attempts on transient network errors (each with a
        # small backoff). Bot-blocked / 4xx responses don't retry —
        # those will fail the same way again and just waste time.
        _MAX_ATTEMPTS = 3
        last_exc: Exception | None = None
        r = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=_timeout,
                    http2=False,  # h2 occasionally hangs on poorly-tuned servers
                ) as c:
                    r = await c.get(url, headers=_BROWSER_HEADERS)
                last_exc = None
                break
            except (
                httpx.ConnectError, httpx.ReadTimeout,
                httpx.RemoteProtocolError, httpx.ConnectTimeout,
                httpx.PoolTimeout,
            ) as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    # Exponential backoff: 0.5s, 1s before retry.
                    await _asyncio.sleep(0.5 * (2 ** attempt))
                    continue
            except httpx.HTTPError as exc:
                # Non-transient HTTP error (TLS verify fail, invalid
                # URL, etc.) — surface immediately, no retry.
                last_exc = exc
                break
        if r is None and last_exc is not None:
            # B-233 + 2026-05-28: structured error with retry count
            # and fallback hint. Pre-fix the agent kept hammering the
            # same URL because the message gave no actionable info.
            err_msg = str(last_exc) or repr(last_exc)
            return _fail(
                call, t0,
                f"http error after {_MAX_ATTEMPTS} attempts: "
                f"{type(last_exc).__name__}: {err_msg}. "
                f"If this is a site that bot-blocks (Cloudflare, "
                f"Akamai), try ``browser_open(url=..., visible=true)`` "
                f"or ``browser_use_my_browser(url=...)`` to fetch via "
                f"a real Chromium — those defeat most fingerprint "
                f"checks the raw HTTP client trips on.",
            )
        assert r is not None  # mypy/runtime — guard above ensures this

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
        ok = 200 <= r.status_code < 400
        # 2026-05-28: when a public site returns a 4xx that looks
        # like anti-bot (403 / 429 / 503 with "cloudflare" / "captcha"
        # in body), point the agent at browser_open. Today the agent
        # often retries the exact same web_fetch and loses budget.
        err = None
        if not ok:
            err = f"HTTP {r.status_code}"
            if r.status_code in (403, 429, 503):
                lower_body = (text or "")[:2000].lower()
                if any(
                    needle in lower_body
                    for needle in (
                        "cloudflare", "just a moment", "captcha",
                        "ddos protection", "access denied",
                        "请完成验证", "人机验证",
                    )
                ):
                    err += (
                        " — looks like bot-blocked; retry with "
                        "``browser_open(url=..., visible=true)`` "
                        "or ``browser_use_my_browser(url=...)`` "
                        "for a real Chromium session."
                    )
        return ToolResult(
            call_id=call.id,
            ok=ok,
            content=content,
            error=err,
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
        # 2026-05-28: multi-engine fallback for CN networks. DDG is
        # often unreachable from mainland China; when the configured
        # backend either errors OR returns 0 results, automatically
        # try Bing CN HTML (no key, reachable in CN). The agent never
        # has to know — it just gets results. Disable via
        # ``cfg.disable_fallback = true``.
        disable_fallback = bool(cfg.get("disable_fallback", False))
        used_engines: list[str] = []
        last_error: str | None = None
        results: list[dict[str, str]] = []

        async def _try_engine(name: str):
            used_engines.append(name)
            if name == "ddg":
                return await self._search_ddg(query.strip(), max_results)
            if name == "bing_cn":
                return await self._search_bing_cn_html(
                    query.strip(), max_results,
                )
            if name == "bing":
                return await self._search_bing(
                    query.strip(), max_results, cfg,
                )
            if name == "brave":
                return await self._search_brave(
                    query.strip(), max_results, cfg,
                )
            if name == "google_cse":
                return await self._search_google_cse(
                    query.strip(), max_results, cfg,
                )
            raise _SearchBackendError(f"unknown engine: {name}")

        # Build the engine try-order. Primary = configured provider;
        # if primary is DDG (the default) and fallback is enabled,
        # tack Bing CN on the end.
        try_order = [provider]
        if not disable_fallback:
            if provider == "ddg" and "bing_cn" not in try_order:
                try_order.append("bing_cn")
            elif provider == "bing_cn" and "ddg" not in try_order:
                try_order.append("ddg")  # other direction too

        for engine in try_order:
            try:
                results = await _try_engine(engine)
            except _SearchBackendError as exc:
                last_error = f"{engine}: {exc}"
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = (
                    f"{engine}: {type(exc).__name__}: {exc}"
                )
                continue
            if results:
                break

        if not results:
            # All engines failed or returned 0. Surface the LAST error
            # so the agent has something concrete + which engines we
            # tried (so it doesn't pick the same one on retry).
            if last_error is not None:
                return _fail(
                    call, t0,
                    f"search failed across engines {used_engines}: "
                    f"{last_error}. If this is from a CN network and "
                    f"DDG is blocked, configure a Bing API key via "
                    f"``evolution.search.bing_api_key`` for higher "
                    f"quality results — Bing CN HTML scrape is the "
                    f"current fallback but is rate-limited.",
                )
            return ToolResult(
                call_id=call.id, ok=True,
                content=(
                    f"(no results for {query!r} via "
                    f"{', '.join(used_engines)})"
                ),
                side_effects=(),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )

        # Tag which engine actually returned the results — useful
        # debug signal in chat ("via bing_cn (DDG was unreachable)").
        winning_engine = used_engines[-1] if used_engines else provider
        blocks = [
            f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ]
        # If primary failed and fallback won, surface that — saves the
        # agent a context cycle of "why am I getting Bing results?".
        engine_note = (
            f" (via {winning_engine})"
            if winning_engine == provider
            else (
                f" (via {winning_engine}; primary {provider} "
                f"unreachable: {last_error})"
            )
        )
        return ToolResult(
            call_id=call.id, ok=True,
            content=(
                f"{len(results)} results for {query!r}{engine_note}:"
                "\n\n" + "\n\n".join(blocks)
            ),
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _search_ddg(
        self, query: str, max_results: int,
    ) -> list[dict[str, str]]:
        """2026-05-28: fail-fast on DDG so CN-network users hit the
        Bing CN fallback in ~8s instead of hanging 15-30s on a
        connection that's never going to succeed. UA upgraded to real
        Chrome so the few cases DDG IS reachable don't return CAPTCHA."""
        import httpx
        url = "https://duckduckgo.com/html/"
        # Use the same browser-realistic headers as web_fetch so DDG
        # doesn't 403 / CAPTCHA us on UA fingerprint alone.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        }
        # Tight connect timeout — DDG is either reachable in 5s or
        # not at all (most often "not at all" from CN networks).
        timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
        ) as c:
            r = await c.post(url, data={"q": query}, headers=headers)
        if r.status_code != 200:
            raise _SearchBackendError(
                f"DDG returned HTTP {r.status_code}"
            )
        return _parse_ddg_html(r.text, max_results)

    async def _search_bing_cn_html(
        self, query: str, max_results: int,
    ) -> list[dict[str, str]]:
        """2026-05-28: Bing CN HTML scrape — no API key, reachable
        from CN networks where DDG is blocked. Result shape matches
        the other backends ({title, url, snippet}).

        Endpoint is ``cn.bing.com`` (not ``www.bing.com``) — the CN
        host is friendlier to mainland networks and returns the
        same SERP HTML.
        """
        import httpx
        import re
        from urllib.parse import quote_plus
        endpoint = (
            f"https://cn.bing.com/search?q={quote_plus(query)}&form=QBLH"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        }
        timeout = httpx.Timeout(connect=8.0, read=15.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout,
        ) as c:
            r = await c.get(endpoint, headers=headers)
        if r.status_code != 200:
            raise _SearchBackendError(
                f"Bing CN returned HTTP {r.status_code}"
            )
        return _parse_bing_html(r.text, max_results)

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

