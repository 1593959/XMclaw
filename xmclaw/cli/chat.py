"""Interactive REPL client for the v2 daemon.

``xmclaw chat`` — connects a WS to an already-running daemon,
prompts the user at the terminal, sends user messages as JSON frames,
and renders the streaming ``BehavioralEvent`` flow back as a readable
conversation:

    > user: summarize /tmp/doc.txt briefly
      ~ thinking...
      → file_read({"path": "/tmp/doc.txt"})
      ← (file contents returned)
      ~ thinking...
    ◉ agent: The document describes …

Turn end is detected by quiet period — when no new event arrives for
``QUIET_MS`` after the last one, the client assumes the agent turn is
complete and prompts the user again. Works correctly for both
single-hop (plain text) and multi-hop (tool) turns.

Exit cleanly on Ctrl+C, Ctrl+D, or typing ``/quit``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any


# Quiet period (seconds). After the last event, wait this long; if no
# new event arrives, treat the turn as finished and prompt the user.
# Set generously so slow LLMs (multi-second TTFB) don't trip a false
# turn-end between user_message and llm_response.
QUIET_MS = 3.0


@dataclass(frozen=True, slots=True)
class RenderedLine:
    """Result of formatting one event into something a terminal can show."""

    text: str                      # the line (already prefixed / shaped)
    is_assistant: bool = False     # True for the final-ish assistant text


# ANSI color helpers
_C_RESET = "\x1b[0m"
_C_GREEN = "\x1b[32m"
_C_YELLOW = "\x1b[33m"
_C_RED = "\x1b[31m"
_C_CYAN = "\x1b[36m"
_C_DIM = "\x1b[2m"
_C_BOLD = "\x1b[1m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{_C_RESET}"


def _highlight_md_code_blocks(text: str) -> str:
    """Wave-33: syntax-highlight fenced code blocks in terminal output.

    Best-effort: if pygments is available, highlight each ``` block
    with its declared language. Falls back to plain text silently.
    """
    try:
        from pygments import highlight as _pygments_highlight
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.formatters import TerminalFormatter
    except Exception:  # noqa: BLE001
        return text

    import re

    def _repl(m: "re.Match[str]") -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        if not code.strip():
            return m.group(0)
        try:
            if lang:
                lexer = get_lexer_by_name(lang, stripall=True)
            else:
                lexer = guess_lexer(code)
        except Exception:  # noqa: BLE001
            return m.group(0)
        try:
            highlighted = _pygments_highlight(code, lexer, TerminalFormatter())
            return f"```\n{highlighted.rstrip()}\n```"
        except Exception:  # noqa: BLE001
            return m.group(0)

    return re.sub(r"```(\w*)\n(.*?)```", _repl, text, flags=re.DOTALL)


def format_event(event: dict[str, Any]) -> RenderedLine | None:
    """Turn a single BehavioralEvent JSON frame into one terminal line.

    Returns ``None`` when the event is best left silent (e.g.
    USER_MESSAGE echo of what the user just typed; verbose internal
    state). Keeps the terminal conversation readable.

    This function is pure — no IO — so it's unit-testable without a
    live daemon.
    """
    etype = event.get("type", "")
    payload = event.get("payload") or {}

    if etype == "user_message":
        # Suppress echo — user already sees their own line in the REPL.
        return None

    if etype == "llm_request":
        hop = payload.get("hop")
        model = payload.get("model", "")
        meta_parts: list[str] = []
        if hop is not None:
            meta_parts.append(f"hop {hop}")
        if model:
            meta_parts.append(model)
        meta = f"  ({', '.join(meta_parts)})" if meta_parts else ""
        if hop is not None and hop == 0:
            return RenderedLine(text=f"  ~ thinking...{_C_DIM}{meta}{_C_RESET}")
        return RenderedLine(text=f"  ~ thinking...{_C_DIM}{meta}{_C_RESET}")

    if etype == "llm_response":
        if not payload.get("ok", True):
            err = payload.get("error", "unknown error")
            return RenderedLine(text=_color(f"  [WARN] llm error: {err}", _C_RED))
        tool_calls_count = int(payload.get("tool_calls_count", 0) or 0)
        content = payload.get("content") or ""
        if tool_calls_count == 0 and content.strip():
            # Wave-33: syntax-highlight code blocks in terminal.
            rendered = _highlight_md_code_blocks(content)
            return RenderedLine(
                text=f"> agent: {rendered}",
                is_assistant=True,
            )
        return None

    if etype == "tool_call_emitted":
        name = payload.get("name", "?")
        args = payload.get("args", {})
        args_short = json.dumps(args, ensure_ascii=False)
        if len(args_short) > 80:
            args_short = args_short[:77] + "..."
        return RenderedLine(
            text=f"  {_color('->', _C_YELLOW)} {_color(name, _C_BOLD)}({args_short})"
        )

    if etype == "tool_invocation_started":
        name = payload.get("name", "?")
        return RenderedLine(
            text=f"  {_color('->', _C_YELLOW)} {_color(name, _C_BOLD)} {_C_DIM}(running...){_C_RESET}"
        )

    if etype == "tool_invocation_finished":
        name = payload.get("name", "?")
        ok = payload.get("ok", True)
        if not ok:
            err = payload.get("error", "")
            return RenderedLine(
                text=f"  {_color('<-', _C_RED)} {_color(name, _C_BOLD)} failed: {err}"
            )
        result = payload.get("result")
        side_effects = payload.get("expected_side_effects") or []
        if side_effects:
            return RenderedLine(
                text=f"  {_color('<-', _C_GREEN)} {_color(name, _C_BOLD)} ok, wrote: {side_effects}"
            )
        summary: str
        if isinstance(result, str):
            summary = result if len(result) < 80 else result[:77] + "..."
        else:
            summary = type(result).__name__
        return RenderedLine(
            text=f"  {_color('<-', _C_GREEN)} {_color(name, _C_BOLD)} ok: {summary}"
        )

    # Wave-33: Canvas Artifact events for terminal
    if etype == "canvas_artifact_created":
        kind = payload.get("kind", "")
        title = payload.get("title", "")
        return RenderedLine(
            text=f"  {_color('[canvas]', _C_CYAN)} {kind}: {title}"
        )

    if etype == "canvas_artifact_updated":
        art_id = payload.get("artifact_id", "")
        return RenderedLine(
            text=f"  {_color('[canvas]', _C_CYAN)} updated {art_id[:20]}"
        )

    if etype == "canvas_artifact_closed":
        art_id = payload.get("artifact_id", "")
        return RenderedLine(
            text=f"  {_color('[canvas]', _C_CYAN)} closed {art_id[:20]}"
        )

    if etype == "anti_req_violation":
        msg = payload.get("message", "unspecified")
        return RenderedLine(text=_color(f"  [WARN] violation: {msg}", _C_RED))

    if etype == "session_lifecycle":
        phase = payload.get("phase", "?")
        if phase == "create":
            return RenderedLine(text="  (session opened)")
        if phase == "destroy":
            return RenderedLine(text="  (session closed)")
        return None

    # Evolution / skill-promotion flashes.
    if etype == "skill_promoted":
        skill_id = payload.get("skill_id", "?")
        fv = payload.get("from_version")
        tv = payload.get("to_version")
        return RenderedLine(
            text=f"  {_color('[evolved]', _C_GREEN)} {skill_id} v{fv}→v{tv}",
        )

    if etype == "skill_rolled_back":
        skill_id = payload.get("skill_id", "?")
        fv = payload.get("from_version")
        tv = payload.get("to_version")
        reason = payload.get("reason") or ""
        tail = f": {reason}" if reason else ""
        return RenderedLine(
            text=f"  {_color('[rolled back]', _C_YELLOW)} {skill_id} v{fv}→v{tv}{tail}",
        )

    if etype == "skill_candidate_proposed":
        skill_id = payload.get("winner_candidate_id", "?")
        ver = payload.get("winner_version")
        return RenderedLine(
            text=f"  {_color('[candidate]', _C_DIM)} {skill_id} v{ver} proposed",
        )

    return None  # unknown types — stay quiet


async def _read_loop(
    ws: Any,
    inbox: asyncio.Queue[Any],
    stop: asyncio.Event,
) -> None:
    """Drain WS messages into an asyncio.Queue. Signals stop on close."""
    try:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await inbox.put(frame)
    except Exception:  # noqa: BLE001 — WS closed, network hiccup, etc.
        pass
    finally:
        stop.set()


async def _drain_until_quiet(
    inbox: asyncio.Queue[Any],
    *,
    quiet: float = QUIET_MS,
    overall_timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Pull events until the turn is finished.

    Turn-end is decided by event sequence, not just timing:
      * a terminal ``llm_response`` (``tool_calls_count == 0``) ends it
        immediately — that's the assistant's final answer.
      * after ``llm_request`` or ``tool_call_emitted`` we *know* a
        response/result is on the way, so we wait up to the overall
        timeout, not the short quiet window.
      * otherwise (post-tool-result, mid-hop) we fall back to the quiet
        window, which catches odd cases without hanging.
    ``overall_timeout`` is the hard cap so a broken agent can't hang
    the REPL forever.
    """
    events: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + overall_timeout
    awaiting_response = False
    while True:
        now = asyncio.get_running_loop().time()
        if now >= deadline:
            return events
        if not events or awaiting_response:
            wait = deadline - now
        else:
            wait = quiet
        try:
            ev = await asyncio.wait_for(inbox.get(), timeout=wait)
        except asyncio.TimeoutError:
            return events
        events.append(ev)
        etype = ev.get("type")
        if etype == "llm_response":
            payload = ev.get("payload") or {}
            if int(payload.get("tool_calls_count", 0) or 0) == 0:
                return events
            awaiting_response = False
        elif etype in ("llm_request", "tool_call_emitted"):
            awaiting_response = True
        else:
            awaiting_response = False


async def _chat_loop(url: str, session_id: str) -> int:
    """Main REPL: connect, then loop {prompt → send → drain → render}."""
    import websockets

    # Windows terminals default to cp936 (GBK) which cannot encode
    # several Unicode symbols used in format_event.  Force UTF-8 so
    # the REPL doesn't crash with UnicodeEncodeError mid-conversation.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"connecting to {url} ...")
    try:
        ws = await websockets.connect(url, open_timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        print(f"connection failed: {exc}")
        print("  is the daemon running? try `xmclaw serve` in another terminal.")
        return 1

    inbox: asyncio.Queue[Any] = asyncio.Queue()
    stop = asyncio.Event()
    reader = asyncio.create_task(_read_loop(ws, inbox, stop))

    try:
        # Drain the initial session_create frame (if any).
        initial = await _drain_until_quiet(inbox, quiet=0.3, overall_timeout=2.0)
        for ev in initial:
            line = format_event(ev)
            if line is not None:
                print(line.text)

        print(f"session: {session_id}   (type /quit to exit)")

        # Use a background thread for stdin so the event loop stays free
        # to process WS messages while the user is typing.
        stdin_queue: asyncio.Queue[str | None] = asyncio.Queue()
        stdin_stop = threading.Event()

        def _stdin_reader() -> None:
            while not stdin_stop.is_set():
                try:
                    line = sys.stdin.readline()
                except (EOFError, KeyboardInterrupt):
                    stdin_queue.put_nowait(None)
                    return
                if not line:
                    stdin_queue.put_nowait(None)
                    return
                stdin_queue.put_nowait(line)

        stdin_thread = threading.Thread(target=_stdin_reader, daemon=True)
        stdin_thread.start()

        while not stop.is_set():
            # Poll for user input with a short timeout so daemon events
            # that arrive while the user is typing are rendered immediately.
            try:
                user = await asyncio.wait_for(stdin_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # No input yet — drain any daemon events so the user
                # sees them right away instead of only after pressing Enter.
                while not inbox.empty():
                    ev = inbox.get_nowait()
                    line = format_event(ev)
                    if line is not None:
                        print(line.text)
                continue

            if user is None:  # EOF
                break
            user = user.strip()
            if user in ("/quit", "/exit", "/q"):
                break
            if not user:
                continue

            frame = {"type": "user", "content": user}
            try:
                await ws.send(json.dumps(frame))
            except Exception as exc:  # noqa: BLE001
                print(f"send failed: {exc}")
                break

            events = await _drain_until_quiet(inbox)
            if not events:
                print("  (no response — daemon idle or agent disabled?)")
                continue
            for ev in events:
                line = format_event(ev)
                if line is not None:
                    print(line.text)

        stdin_stop.set()
        return 0
    finally:
        reader.cancel()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def pick_resume_session(*, prefer_last: bool = False, limit: int = 10) -> str | None:
    """Return a session_id to resume, or ``None`` if no saved sessions.

    With ``prefer_last=True`` skip the picker and just return the most
    recently active session. Otherwise print a numbered list and prompt
    on stdin.
    """
    from datetime import datetime

    from xmclaw.daemon.session_store import SessionStore
    from xmclaw.utils.paths import default_sessions_db_path

    try:
        store = SessionStore(default_sessions_db_path())
        rows = store.list_recent(limit=limit)
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    if prefer_last:
        return rows[0]["session_id"]

    print("recent sessions:")
    for i, r in enumerate(rows, start=1):
        when = datetime.fromtimestamp(r["updated_at"]).strftime("%Y-%m-%d %H:%M")
        print(f"  {i:>2}. {r['session_id']:24}  {r['message_count']:>3} msgs   {when}")
    print()
    try:
        choice = input("pick a number (Enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice:
        return None
    try:
        idx = int(choice)
    except ValueError:
        print(f"  not a number: {choice!r}")
        return None
    if idx < 1 or idx > len(rows):
        print(f"  out of range: {idx}")
        return None
    return rows[idx - 1]["session_id"]


def run_chat(
    *,
    url: str | None = None,
    session_id: str | None = None,
    token: str | None = None,
) -> int:
    """Entry point called by the CLI command. Returns a process exit code.

    ``token`` is the pairing secret that the daemon's auth_check
    compares against. We attach it as a ``?token=<value>`` query
    parameter; see xmclaw/daemon/app.py for extraction.
    """
    effective_url = url or "ws://127.0.0.1:8765/agent/v2/{session_id}"
    sid = session_id or f"chat-{uuid.uuid4().hex[:8]}"
    effective_url = effective_url.replace("{session_id}", sid)
    if token:
        import urllib.parse as _up
        sep = "&" if "?" in effective_url else "?"
        effective_url = f"{effective_url}{sep}token={_up.quote(token)}"
    try:
        return asyncio.run(_chat_loop(effective_url, sid))
    except KeyboardInterrupt:
        print()  # newline after ^C
        return 130
