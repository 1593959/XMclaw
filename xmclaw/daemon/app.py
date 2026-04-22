"""v2 daemon — FastAPI app exposing the v2 event bus over WebSocket.

Phase 4.0 delivery. Minimal end-to-end app: health check + one WS
endpoint that proxies user messages into the bus and streams
behavioral events back out as NDJSON frames. LLM wiring is NOT here
yet — Phase 4.1 layers the scheduler / grader / skills stack on top.

This is the first place v2 emerges as a RUNNING SERVICE rather than a
test-harness: ``xmclaw v2 serve`` starts it, and any WS client can
connect.

Anti-req #8 (device-bound auth on WS) stays advisory here —
``auth_check`` argument on the factory hooks in the enforcement path.
Phase 4.x replaces the default accept-all with ed25519 pairing.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from xmclaw import __version__
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
    make_event,
)


def create_app(
    *,
    bus: InProcessEventBus | None = None,
    auth_check: Callable[[str | None], Awaitable[bool]] | None = None,
    agent: AgentLoop | None = None,
    config: dict[str, Any] | None = None,
) -> FastAPI:
    """Build the v2 FastAPI app.

    Parameters
    ----------
    bus : InProcessEventBus | None
        Event bus to use. Defaults to a fresh in-process instance so
        each ``create_app`` call gets an isolated bus — useful for
        tests. Production callers should pass a shared bus.
    auth_check : callable | None
        Async ``(token: str | None) -> bool`` for anti-req #8 pairing.
        The server extracts the token from either the ``token`` query
        parameter or an ``Authorization: Bearer <token>`` header. When
        ``auth_check`` is set, a missing or failed token closes the WS
        with code 4401. Default (``None``) accepts all connections —
        safe only on loopback.
    agent : AgentLoop | None
        Optional agent turn orchestrator. When provided, user messages
        trigger ``agent.run_turn`` (LLM ↔ tool loop); events flow back
        via the bus subscription.
    config : dict | None
        Optional config dict (``daemon/config.json`` shape). If
        ``agent`` is not provided but ``config`` is, the factory tries
        to build an AgentLoop from the config's LLM section. This is
        the usable-out-of-the-box path for ``xmclaw v2 serve``.

    Precedence: explicit ``agent=`` wins over ``config=``. If neither
    is given, the daemon runs in Phase 4.0 echo mode — useful for
    WS-plumbing tests and clients that manage their own reasoning
    upstream.
    """
    app = FastAPI(title="XMclaw v2 daemon", version=__version__)
    bus = bus or InProcessEventBus()
    app.state.bus = bus

    if agent is None and config is not None:
        # Local import avoids a circular dep (factory imports from this
        # module's sibling packages).
        from xmclaw.daemon.factory import build_agent_from_config
        agent = build_agent_from_config(config, bus)
    app.state.agent = agent

    @app.get("/health")
    async def health() -> JSONResponse:
        """Cheap liveness probe — confirms the app is responsive."""
        return JSONResponse({
            "status": "ok",
            "version": __version__,
            "bus": type(bus).__name__,
        })

    # ── /api/v2/pair ──
    # Returns the pairing token (or null in --no-auth mode) to the UI
    # so users don't have to paste the token from disk. Security posture:
    # this endpoint has NO CORS headers set, so browsers enforce the
    # same-origin policy — a page at evil.com cannot fetch this URL
    # from the user's browser. Same-origin pages (our own UI at
    # /ui/*) can read it. Another process on localhost can curl this,
    # but that threat was already outside anti-req #8's scope (a
    # local-user process can also cat ~/.xmclaw/v2/pairing_token.txt).
    @app.get("/api/v2/pair")
    async def pair() -> JSONResponse:
        token: str | None = None
        if auth_check is not None:
            # Read the pairing file from the same location the daemon
            # created it in. Local import to avoid coupling the app
            # module to the pairing module's surface.
            try:
                from xmclaw.daemon.pairing import default_token_path
                token_path = default_token_path()
                if token_path.exists():
                    token = token_path.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                token = None
        return JSONResponse({"token": token})

    # ── /ui/ static files + root redirect ──
    # Phase 4.6: serve a single-page UI bundled with the package, so
    # users can open `http://127.0.0.1:8765/` in a browser and get a
    # working chat interface. The UI files live in
    # xmclaw/daemon/static and are not auth-gated — the WebSocket
    # the UI connects to still requires the pairing token.
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount(
            "/ui",
            StaticFiles(directory=str(_static_dir), html=True),
            name="ui",
        )

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse(url="/ui/", status_code=302)

    @app.websocket("/agent/v2/{session_id}")
    async def agent_ws(ws: WebSocket, session_id: str) -> None:
        # Anti-req #8 gate. Token arrives either as a query param
        # (browsers can't set WS headers) or an Authorization: Bearer
        # header (CLIs / SDKs). We check both so we don't force one
        # choice on every kind of client.
        if auth_check is not None:
            token: str | None = ws.query_params.get("token")
            if not token:
                auth_header = ws.headers.get("authorization", "") or ""
                if auth_header.lower().startswith("bearer "):
                    token = auth_header[len("bearer "):].strip() or None
            ok = False
            try:
                ok = await auth_check(token)
            except Exception:  # noqa: BLE001 — auth must never crash daemon
                ok = False
            if not ok:
                # WebSocket protocol needs accept() before close(), or the
                # client gets a bare TCP reset with no close code. We want
                # 4401 visible to the client, so accept then close.
                await ws.accept()
                await ws.close(code=4401, reason="unauthorized")
                return

        await ws.accept()

        # Subscribe this connection to the bus BEFORE the lifecycle event
        # so the client sees its own session-create frame.
        outbox: list[BehavioralEvent] = []

        async def forward(event: BehavioralEvent) -> None:
            # Only forward events relevant to this session to avoid
            # leaking across agents on the same daemon.
            if event.session_id != session_id:
                return
            outbox.append(event)
            try:
                await ws.send_text(json.dumps({
                    "id": event.id,
                    "ts": event.ts,
                    "session_id": event.session_id,
                    "agent_id": event.agent_id,
                    "type": event.type.value,
                    "payload": event.payload,
                    "correlation_id": event.correlation_id,
                    "parent_id": event.parent_id,
                    "schema_version": event.schema_version,
                }))
            except Exception:  # noqa: BLE001 — socket might close mid-send
                pass

        sub = bus.subscribe(
            lambda e: e.session_id == session_id,
            forward,
        )

        # Announce the session.
        await bus.publish(make_event(
            session_id=session_id, agent_id="daemon",
            type=EventType.SESSION_LIFECYCLE,
            payload={"phase": "create", "via": "ws"},
        ))
        await bus.drain()

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    frame: Any = json.loads(raw)
                except json.JSONDecodeError:
                    # Drop malformed frames; connection stays open.
                    continue
                if not isinstance(frame, dict):
                    continue
                # Frame shape: {"type": "user", "content": "..."}
                if frame.get("type") == "user":
                    content = str(frame.get("content", ""))
                    if agent is not None:
                        # Phase 4.1: run the full LLM ↔ tool loop. The
                        # AgentLoop publishes USER_MESSAGE + every LLM /
                        # tool event onto the bus; our subscription
                        # forwards them to this WS.
                        try:
                            await agent.run_turn(session_id, content)
                        except Exception as exc:  # noqa: BLE001
                            # Surface a structured error frame so the
                            # client sees the failure instead of a
                            # silent socket stall.
                            await bus.publish(make_event(
                                session_id=session_id, agent_id="daemon",
                                type=EventType.ANTI_REQ_VIOLATION,
                                payload={
                                    "message": f"agent loop crashed: {type(exc).__name__}: {exc}",
                                },
                            ))
                            await bus.drain()
                    else:
                        # Phase 4.0 fallback: plain bus-echo for tests
                        # and for clients that do their own reasoning.
                        await bus.publish(make_event(
                            session_id=session_id, agent_id="daemon",
                            type=EventType.USER_MESSAGE,
                            payload={
                                "content": content,
                                "channel": "ws",
                            },
                        ))
                        await bus.drain()
                # Other frame types are silently ignored for now.
                # Phase 4.2+ will add cancel / ask_user_answer / etc.
        except WebSocketDisconnect:
            pass
        finally:
            sub.cancel()
            # Do NOT wipe session history on disconnect -- browser refresh
            # is a WS close, and the user's prior exchanges must survive
            # it. History stays in the AgentLoop's in-memory dict keyed
            # by session_id. Explicit reset is via agent.clear_session()
            # which the UI triggers with a /reset intent (not on close).
            #
            # Bounded by AgentLoop.history_cap (default 40 messages per
            # session). Sessions created and then never reconnected do
            # leak until the daemon restarts -- acceptable for now since
            # sessions are user-created and finite. Cross-process
            # persistence (SQLite-backed session store) lands later.
            await bus.publish(make_event(
                session_id=session_id, agent_id="daemon",
                type=EventType.SESSION_LIFECYCLE,
                payload={"phase": "destroy", "via": "ws"},
            ))
            await bus.drain()

    return app


# Convenience: a default app instance for `uvicorn xmclaw.daemon.app:app`.
app = create_app()
