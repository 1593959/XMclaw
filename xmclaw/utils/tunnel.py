"""Cloudflared tunnel auto-start.

Direct port of QwenPaw's ``src/qwenpaw/tunnel/cloudflare.py``. When a
webhook-driven channel (Feishu / DingTalk / WeCom / Telegram) is
enabled, the daemon needs a public URL to receive callbacks. Cloudflare
Tunnel via ``cloudflared`` binary is the simplest no-config-required
path — it starts a temporary tunnel and prints a public ``trycloudflare.com``
URL on stderr; we parse it out and hand the URL to the channel
adapters during their pairing step.

Public API:
* :func:`is_cloudflared_available` — bool, true if ``cloudflared`` is
  on PATH
* :class:`TunnelManager` — start / stop / get_url

The manager is best-effort: if the binary is missing or the tunnel
fails to bring up, the channel adapters that asked for a tunnel get
``None`` and should surface a clear "configure cloudflared or set
``QWENPAW_PUBLIC_URL`` manually" error to the user.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil

_log = logging.getLogger(__name__)

# cloudflared prints lines like:
#   2026-04-26T00:00:00Z INF +-...
#   ...| https://example-some-words.trycloudflare.com  |
# Pattern matches the public URL within stderr/stdout lines.
_TUNNEL_URL_RE = re.compile(
    r"https://[a-z0-9\-]+\.trycloudflare\.com",
    re.IGNORECASE,
)


def is_cloudflared_available() -> bool:
    """Check whether ``cloudflared`` is on PATH."""
    return shutil.which("cloudflared") is not None


class TunnelStartTimeout(RuntimeError):
    """Raised when cloudflared doesn't print a tunnel URL within timeout."""


class TunnelManager:
    """Start / stop a cloudflared quick-tunnel pointing at ``localhost:port``.

    Use::

        mgr = TunnelManager(port=8765)
        await mgr.start()
        url = mgr.url  # e.g. "https://abc-def.trycloudflare.com"
        ...
        await mgr.stop()
    """

    def __init__(
        self,
        *,
        port: int,
        host: str = "127.0.0.1",
        binary: str = "cloudflared",
        start_timeout_s: float = 30.0,
    ) -> None:
        self._port = port
        self._host = host
        self._binary = binary
        self._start_timeout_s = start_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._url: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._url_event = asyncio.Event()

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> str:
        """Start the tunnel and wait until the public URL is announced.

        Raises:
            FileNotFoundError: cloudflared binary missing
            TunnelStartTimeout: URL wasn't printed within
                ``start_timeout_s``
        """
        if self.is_running and self._url:
            return self._url
        if shutil.which(self._binary) is None:
            raise FileNotFoundError(
                f"{self._binary!r} not found on PATH. "
                "Install cloudflared (https://github.com/cloudflare/cloudflared) "
                "or set the public URL manually via channel config."
            )

        self._url_event = asyncio.Event()
        self._url = None
        cmd = [
            self._binary, "tunnel", "--no-autoupdate",
            "--url", f"http://{self._host}:{self._port}",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(self._read_output())

        try:
            await asyncio.wait_for(
                self._url_event.wait(), timeout=self._start_timeout_s
            )
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise TunnelStartTimeout(
                f"cloudflared did not print a tunnel URL within "
                f"{self._start_timeout_s}s"
            ) from exc

        assert self._url is not None
        _log.info("cloudflared tunnel up: %s", self._url)
        return self._url

    async def stop(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        self._proc = None
        self._url = None

    async def _read_output(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    return
                try:
                    line = raw.decode("utf-8", errors="replace")
                except UnicodeDecodeError:
                    continue
                # Log the tunnel chatter at debug — useful for
                # troubleshooting without flooding INFO.
                _log.debug("cloudflared: %s", line.rstrip())
                if self._url is None:
                    match = _TUNNEL_URL_RE.search(line)
                    if match:
                        self._url = match.group(0)
                        self._url_event.set()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning("cloudflared.reader_failed: %s", exc)
