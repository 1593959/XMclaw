"""Chat CLI — ``xmclaw chat [--plain] [--session ID]``."""
from __future__ import annotations

import urllib.parse

import typer

chat_app = typer.Typer(help="Launch interactive chat (TUI by default)")


def _http_base_from_ws(ws_url: str) -> str:
    """Derive HTTP base URL from a WebSocket URL.

    ws://host:port/path  -> http://host:port
    wss://host:port/path -> https://host:port
    """
    parsed = urllib.parse.urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    netloc = parsed.netloc
    return f"{scheme}://{netloc}"


def _fetch_token(http_base: str) -> str | None:
    """Fetch the pairing token from the daemon's /api/v2/pair endpoint.

    Returns the token hex string, or None if the daemon is in --no-auth
    mode (endpoint returns {"token": null}).
    """
    import urllib.request

    url = f"{http_base}/api/v2/pair"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
            token = data.get("token")
            return token if token else None
    except Exception:
        return None


def _ws_url_with_token(ws_url: str, token: str | None) -> str:
    """Append ?token=... to the WS URL when a token is available."""
    if not token:
        return ws_url
    sep = "&" if "?" in ws_url else "?"
    return f"{ws_url}{sep}token={urllib.parse.quote(token, safe='')}"


@chat_app.callback(invoke_without_command=True)
def chat(
    plain: bool = typer.Option(False, "--plain", help="Use basic stdin/stdout instead of TUI"),
    session: str | None = typer.Option(None, "--session", help="Resume a session ID"),
    daemon_url: str = typer.Option("ws://127.0.0.1:8765/agent/v2/default", "--url"),
) -> None:
    """Launch XMclaw chat interface."""
    if plain:
        _run_plain_chat(daemon_url, session)
    else:
        _run_tui(daemon_url, session)


def _run_tui(url: str, session_id: str | None) -> None:
    import sys
    try:
        from xmclaw.tui import JarvisTUI
    except ImportError as exc:
        typer.secho(f"TUI dependencies missing: {exc}", fg=typer.colors.RED, err=True)
        typer.secho("Run: pip install textual websockets", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1)

    # Fetch pairing token so the WS upgrade is authorized.
    token = _fetch_token(_http_base_from_ws(url))
    ws_url = _ws_url_with_token(url, token)
    if token:
        typer.secho("已获取 pairing token", fg=typer.colors.GREEN)

    # Windows: textual Input widget does not support CJK IME in raw mode.
    # Use inline driver (ANSI sequences) which works better in Windows
    # Terminal / modern PowerShell, and warn the user.
    driver = None
    if sys.platform == "win32":
        driver = "inline"
        typer.secho(
            "提示: Windows TUI 中文输入法支持有限，如遇输入问题请用 xmclaw chat --plain",
            fg=typer.colors.YELLOW,
        )

    app = JarvisTUI(daemon_ws_url=ws_url, session_id=session_id)
    app.run(inline=(driver == "inline"))


def _run_plain_chat(url: str, session_id: str | None) -> None:
    import asyncio
    import json

    import typer

    # Fetch pairing token so the WS upgrade is authorized.
    token = _fetch_token(_http_base_from_ws(url))
    ws_url = _ws_url_with_token(url, token)
    if token:
        typer.secho("已获取 pairing token", fg=typer.colors.GREEN)

    sid = session_id or f"cli_{asyncio.get_event_loop().time():.0f}"
    typer.echo(f"Session: {sid}")
    typer.echo("输入消息后回车发送 (Ctrl+C 退出).\n")

    async def _loop() -> None:
        try:
            import websockets
        except ImportError:
            typer.echo("websockets not installed — pip install websockets", err=True)
            return
        async with websockets.connect(ws_url) as ws:
            while True:
                try:
                    text = typer.prompt("你")
                except (EOFError, KeyboardInterrupt):
                    break
                await ws.send(json.dumps({
                    "action": "submit",
                    "session_id": sid,
                    "message": text,
                }))
                # Simple blocking read of one response.
                raw = await ws.recv()
                msg = json.loads(raw)
                payload = msg.get("payload", {})
                typer.echo(f"助手: {payload.get('content', payload)}")

    asyncio.run(_loop())
