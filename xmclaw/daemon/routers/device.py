"""WebSocket route for Android Companion devices.

Endpoint: ``GET /device/v1/{device_id}?token=<pairing_token>``

- Accepts WebSocket connections from the companion app.
- Validates ``pairing_token`` (reuses existing shared-secret auth).
- Registers the connection in ``DeviceRegistry``.
- Routes inbound frames to:
    * ``act.result`` / ``obs.*`` → resolve pending ``send_request`` futures
    * ``user.message`` → inject into AgentLoop as a user turn
    * ``user.approval`` → forward to ``ApprovalService``
    * ``obs.event`` → publish on the event bus as ``DEVICE_EVENT``
    * ``dev.hello`` → store metadata and send ``dev.welcome``

Wiring: registered in ``app.py`` via ``app.include_router(router, prefix="/device")``.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from xmclaw import __version__
from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.device_registry import DeviceRegistry
from xmclaw.daemon.pairing import validate_token
from xmclaw.utils.log import get_logger

log = get_logger(__name__)

router = APIRouter()

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _welcome() -> dict[str, Any]:
    return {
        "v": 1,
        "type": "dev.welcome",
        "req_id": None,
        "ts": time.time(),
        "data": {
            "server_ver": __version__,
            "capabilities": ["ui_tree", "screenshot", "clipboard", "gesture"],
            "heartbeat_s": 20,
        },
    }


async def _check_pairing_token(ws: WebSocket) -> bool:
    """Validate the token passed in the WS query string.

    Reuses the existing shared-secret pairing system.
    Returns ``True`` when the token is valid.
    """
    try:
        from xmclaw.daemon.pairing import load_or_create_token
        expected = load_or_create_token()
    except Exception as exc:  # noqa: BLE001
        log.warning("pairing_token_load_failed", exc=exc)
        return False
    presented = ws.query_params.get("token") if hasattr(ws, "query_params") else None
    if presented is None and hasattr(ws, "scope"):
        # Fallback: parse from scope query string
        scope = ws.scope
        q = scope.get("query_string", b"").decode()
        for part in q.split("&"):
            if part.startswith("token="):
                presented = part[len("token="):]
                break
    return validate_token(expected, presented)


async def _inject_user_message(app: Any, device_id: str, data: dict[str, Any]) -> None:
    """Inject a ``user.message`` from the phone into the AgentLoop.

    Uses a dedicated session per device (``device:<device_id>``) so that
    phone interactions carry their own conversation history and can be
    recalled later. The turn is fire-and-forget — the WebSocket handler
    does not block on the agent response; events stream back via the
    event bus and the phone receives them through the same WS channel.
    """
    agent = getattr(app.state, "agent", None)
    if agent is None:
        log.warning("inject_user_message_no_agent", device_id=device_id)
        return

    session_id = f"device:{device_id}"
    text = data.get("text", "")
    image_urls = data.get("image_urls")
    try:
        asyncio.create_task(
            agent.run_turn(
                session_id=session_id,
                user_message=text,
                user_images=tuple(image_urls) if image_urls else None,
                channel_name="android_companion",
            )
        )
        log.info("user_message_injected", device_id=device_id, session_id=session_id, text_preview=text[:80])
    except Exception as exc:  # noqa: BLE001
        log.warning("inject_user_message_run_turn_failed", device_id=device_id, exc=exc)


async def _handle_approval(approval_service: Any, data: dict[str, Any]) -> None:
    """Handle ``user.approval`` from the phone."""
    decision = data.get("decision")
    request_id = data.get("request_id")
    if not request_id or not decision:
        return
    try:
        if decision in ("allow", "always"):
            await approval_service.approve(request_id)
        elif decision == "deny":
            await approval_service.deny(request_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("approval_respond_failed", exc=exc)


# ------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------

@router.websocket("/v1/{device_id}")
async def device_ws(ws: WebSocket, device_id: str) -> None:
    if not await _check_pairing_token(ws):
        await ws.close(code=4401)
        return

    # Accept and register
    await ws.accept()

    # Resolve registry from app.state (injected by lifespan)
    registry: DeviceRegistry | None = getattr(ws.app.state, "device_registry", None)
    if registry is None:
        log.error("device_registry_not_found_on_app_state")
        await ws.close(code=1011)
        return

    conn = registry.register(device_id, ws)
    log.info("device_ws_accepted", device_id=device_id)

    try:
        while True:
            raw = await ws.receive_json()
            t = raw.get("type")
            data = raw.get("data") or {}
            rid = raw.get("req_id")
            ts = raw.get("ts")

            # 1. Resolve pending requests (act.result / obs.*)
            if t in ("act.result", "obs.tree", "obs.screenshot", "obs.clipboard") and rid:
                conn.resolve(rid, data)
                continue

            # 2. User message from phone → inject into AgentLoop
            if t == "user.message":
                await _inject_user_message(ws.app, device_id, data)
                continue

            # 3. Approval response from phone
            if t == "user.approval":
                approval_service = getattr(ws.app.state, "approval_service", None)
                if approval_service is not None:
                    await _handle_approval(approval_service, data)
                continue

            # 4. Device events (window changed, notification, toast...)
            if t == "obs.event":
                bus: InProcessEventBus | None = getattr(ws.app.state, "bus", None)
                if bus is not None:
                    bus.publish(
                        make_event(
                            session_id="_device",
                            agent_id="daemon",
                            type=EventType.DEVICE_EVENT,
                            payload={"device_id": device_id, **data},
                        )
                    )
                continue

            # 5. Handshake hello
            if t == "dev.hello":
                conn.set_hello(data)
                await conn.send(_welcome())
                continue

            # 6. Unknown / unhandled
            log.debug("device_unhandled_frame", type=t, device_id=device_id)

    except WebSocketDisconnect:
        log.info("device_ws_disconnect", device_id=device_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("device_ws_error", device_id=device_id, exc=exc)
    finally:
        registry.drop(device_id)
