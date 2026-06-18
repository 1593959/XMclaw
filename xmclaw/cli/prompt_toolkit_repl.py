"""prompt_toolkit REPL — rich interactive agent console.

Replaces the legacy sys.stdin.readline() loop with a full-featured
REPL: persistent command history, syntax-highlighted multi-line input,
tab completion, and streaming event display.

Dependency: ``pip install prompt_toolkit>=3.0`` (lightweight, ~2 MB).
Falls back gracefully to the legacy stdin REPL if not installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from xmclaw.utils.log import get_logger
from xmclaw.utils.paths import data_dir

_log = get_logger(__name__)

_HISTORY_FILE = data_dir() / "chat_history.txt"
_HISTORY_SIZE = 1000


def _load_history() -> list[str]:
    try:
        if _HISTORY_FILE.exists():
            return [
                line.strip() for line in
                _HISTORY_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ][-_HISTORY_SIZE:]
    except Exception:
        pass
    return []


def _save_history(history: list[str]) -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(
            "\n".join(h for h in history[-_HISTORY_SIZE:] if h.strip()),
            encoding="utf-8",
        )
    except Exception:
        pass


class PromptToolkitRepl:
    """Async REPL using prompt_toolkit for rich input."""

    def __init__(self, ws_url: str, session_id: str | None = None) -> None:
        self._ws_url = ws_url
        self._session_id = session_id or f"repl_{asyncio.get_event_loop().time():.0f}"
        self._history = _load_history()
        self._running = False

    async def run(self) -> None:
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.styles import Style
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.key_binding import KeyBindings
        except ImportError:
            _log.warning("prompt_toolkit not installed; falling back to stdin REPL")
            return await self._run_legacy()

        import websockets

        history = InMemoryHistory()
        for h in self._history:
            history.append_string(h)

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _(event: Any) -> None:
            """Alt+Enter inserts a newline for multi-line input."""
            event.current_buffer.insert_text("\n")

        completer = WordCompleter([
            "/help", "/quit", "/exit", "/q", "/clear", "/history",
            "/plan", "/model", "/session", "/memory",
        ], ignore_case=True, sentence=True)

        style = Style.from_dict({
            "prompt": "ansicyan bold",
            "input": "",
            "separator": "ansibrightblack",
        })

        session: Any = PromptSession(
            history=history,
            completer=completer,
            key_bindings=kb,
            style=style,
            multiline=False,
            wrap_lines=True,
        )

        self._running = True
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def _ws_reader(ws: Any) -> None:
            try:
                async for raw in ws:
                    try:
                        inbox.put_nowait(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
            except asyncio.CancelledError:
                pass

        async with websockets.connect(self._ws_url) as ws:
            reader_task = asyncio.create_task(_ws_reader(ws))
            self._print_header()

            try:
                while self._running:
                    try:
                        user = await session.prompt_async(
                            [("class:prompt", "> "), ("class:input", "")],
                        )
                    except (EOFError, KeyboardInterrupt):
                        break

                    user = user.strip()
                    if not user:
                        continue

                    if user in ("/quit", "/exit", "/q"):
                        break
                    if user == "/help":
                        self._print_help()
                        continue
                    if user == "/clear":
                        os.system("cls" if sys.platform == "win32" else "clear")
                        continue
                    if user == "/history":
                        for i, h in enumerate(self._history[-20:], 1):
                            print(f"  {i:3d}  {h}")
                        continue

                    self._history.append(user)
                    self._save()

                    await ws.send(json.dumps({
                        "type": "user", "content": user,
                    }))

                    # Drain events while the LLM responds
                    await self._drain_events(inbox)

            finally:
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass

    async def _run_legacy(self) -> None:
        """Fallback: basic stdin REPL without prompt_toolkit."""
        import threading
        import websockets

        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        stdin_queue: asyncio.Queue[str | None] = asyncio.Queue()
        stdin_stop = threading.Event()

        def _reader() -> None:
            while not stdin_stop.is_set():
                try:
                    line = sys.stdin.readline()
                except (EOFError, KeyboardInterrupt):
                    stdin_queue.put_nowait(None)
                    return
                stdin_queue.put_nowait(line if line else None)

        threading.Thread(target=_reader, daemon=True).start()

        async with websockets.connect(self._ws_url) as ws:
            reader_task = asyncio.create_task(self._ws_reader_legacy(ws, inbox))
            self._print_header()

            try:
                while True:
                    try:
                        user = await asyncio.wait_for(stdin_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        await self._drain_events(inbox, quiet=False)
                        continue
                    if user is None:
                        break
                    user = user.strip()
                    if user in ("/quit", "/exit", "/q"):
                        break
                    if not user:
                        continue
                    self._history.append(user)
                    self._save()
                    await ws.send(json.dumps({"type": "user", "content": user}))
                    await self._drain_events(inbox)
            finally:
                reader_task.cancel()

    async def _ws_reader_legacy(self, ws: Any, inbox: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            async for raw in ws:
                try:
                    inbox.put_nowait(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _drain_events(self, inbox: asyncio.Queue[dict[str, Any]], quiet: bool = True) -> None:
        """Drain daemon events and render them. Returns when inbox is idle
        for ``QUIET_MS`` milliseconds."""
        QUIET_MS = 0.8  # shorter than legacy 3.0s — prompt_toolkit is more responsive
        drained = 0
        while True:
            try:
                ev = await asyncio.wait_for(inbox.get(), timeout=QUIET_MS)
            except asyncio.TimeoutError:
                break
            drained += 1
            self._render_event(ev)
        if drained == 0 and not quiet:
            # Show a brief tick so the user knows the daemon is alive
            pass

    @staticmethod
    def _render_event(ev: dict[str, Any]) -> None:
        t = ev.get("type", "")
        p = ev.get("payload", {})
        if t == "llm_chunk":
            text = p.get("content", "")
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
        elif t == "llm_response":
            text = p.get("content", "")
            if text:
                sys.stdout.write(text + "\n")
        elif t == "tool_invocation_started":
            name = p.get("name", "tool")
            sys.stdout.write(f"\n▶ {name}…\n")
        elif t == "tool_invocation_finished":
            name = p.get("name", "tool")
            ok = not p.get("error")
            icon = "✓" if ok else "✗"
            dur = p.get("duration_ms", 0)
            sys.stdout.write(f"  {icon} {name} ({dur:.0f}ms)\n")
        elif t == "proactive_proposal":
            sys.stdout.write(f"\n💡 {p.get('message', '')}\n")
        elif t == "error":
            sys.stdout.write(f"\n✗ {p.get('message', '')}\n")
        sys.stdout.flush()

    def _print_header(self) -> None:
        print(f"XMclaw REPL — session: {self._session_id}")
        print(f"  /help  /quit  /clear  /history")
        print(f"  Alt+Enter for multi-line input")
        print()

    @staticmethod
    def _print_help() -> None:
        print("Commands:")
        print("  /help      Show this help")
        print("  /quit      Exit REPL")
        print("  /clear     Clear screen")
        print("  /history   Show last 20 commands")
        print("  /memory    (coming soon)")
        print()

    def _save(self) -> None:
        _save_history(self._history)
