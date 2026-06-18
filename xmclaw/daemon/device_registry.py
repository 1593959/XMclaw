"""DeviceRegistry — 管理已连接手机的生命周期与请求/应答配对。

Scope:
  * ``DeviceRegistry`` — 全局单例，device_id → DeviceConn 映射
  * ``DeviceConn`` — 单台已连手机，send_request 发下行帧并 await 对应 req_id 的应答

Wiring:
  * 在 ``app_lifespan`` 中构造单例挂 ``app.state.device_registry``
  * ``/device/v1/{device_id}`` WebSocket 路由在 accept 时 register，断开时 drop
  * ``AndroidRemoteToolProvider`` 通过 registry.get(device_id) 获取连接并下发命令
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

from xmclaw.utils.log import get_logger

log = get_logger(__name__)

DEFAULT_DEVICE_TIMEOUT: float = 15.0


class DeviceConn:
    """一台已连接手机。

    ``send_request`` 发下行帧并 await 对应 ``req_id`` 的应答。
    由 reader 在收到 ``act.result`` / ``obs.*`` 时调用 ``resolve`` 解开 future。
    """

    def __init__(self, device_id: str, ws) -> None:
        self.device_id = device_id
        self._ws = ws
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._loop = asyncio.get_running_loop()
        self._hello: dict[str, Any] | None = None
        self._connected_at: float = time.time()

    # ------------------------------------------------------------------
    # Hello / metadata
    # ------------------------------------------------------------------
    def set_hello(self, data: dict[str, Any]) -> None:
        self._hello = data

    @property
    def hello(self) -> dict[str, Any] | None:
        return self._hello

    @property
    def screen(self) -> dict[str, Any] | None:
        if self._hello is None:
            return None
        return self._hello.get("screen")

    @property
    def perms(self) -> dict[str, Any] | None:
        if self._hello is None:
            return None
        return self._hello.get("perms")

    @property
    def connected_at(self) -> float:
        return self._connected_at

    # ------------------------------------------------------------------
    # Request / response pairing
    # ------------------------------------------------------------------
    async def send_request(
        self,
        type_: str,
        data: dict[str, Any],
        *,
        timeout: float = DEFAULT_DEVICE_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a framed request and await the matching response.

        Raises ``asyncio.TimeoutError`` if the device does not respond
        within *timeout* seconds.
        """
        req_id = uuid4().hex
        fut = self._loop.create_future()
        self._pending[req_id] = fut
        frame = {
            "v": 1,
            "type": type_,
            "req_id": req_id,
            "ts": time.time(),
            "data": data,
        }
        try:
            await self._ws.send_json(frame)
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.set_exception(exc)
            raise
        return await asyncio.wait_for(fut, timeout)

    def resolve(self, req_id: str, payload: dict[str, Any]) -> None:
        """Called by the WebSocket reader when an inbound frame carries *req_id*."""
        fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def cancel_all(self, exc: Exception | None = None) -> None:
        """Cancel all pending futures (e.g. on disconnect)."""
        pending = list(self._pending.items())
        self._pending.clear()
        for _req_id, fut in pending:
            if not fut.done():
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.cancel()

    # ------------------------------------------------------------------
    # Low-level send (for non-request frames like welcome)
    # ------------------------------------------------------------------
    async def send(self, frame: dict[str, Any]) -> None:
        await self._ws.send_json(frame)

    # ------------------------------------------------------------------
    # Connection state
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        # Best-effort: WebSocket client implementations vary
        ws = self._ws
        close_code = getattr(ws, "close_code", None)
        return close_code is None

    def __repr__(self) -> str:
        return f"<DeviceConn {self.device_id} open={self.is_open} pending={len(self._pending)}>"


class DeviceRegistry:
    """全局单例：device_id ⇄ 活动连接 + 请求/应答配对。"""

    def __init__(self) -> None:
        self._conns: dict[str, DeviceConn] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def register(self, device_id: str, ws) -> DeviceConn:
        """Register a new WebSocket connection for *device_id*."""
        conn = DeviceConn(device_id, ws)
        self._conns[device_id] = conn
        log.info("device_registered", device_id=device_id)
        return conn

    def drop(self, device_id: str) -> None:
        """Remove a connection and cancel all pending futures."""
        conn = self._conns.pop(device_id, None)
        if conn is not None:
            conn.cancel_all(DeviceDisconnected(f"device {device_id} disconnected"))
            log.info("device_dropped", device_id=device_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get(self, device_id: str | None) -> DeviceConn | None:
        """Look up a connection by *device_id*.

        If *device_id* is ``None`` and only one device is connected,
        return that device (single-device convenience).
        """
        if device_id is not None:
            return self._conns.get(device_id)
        if len(self._conns) == 1:
            return next(iter(self._conns.values()))
        return None

    def list(self) -> list[dict[str, Any]]:
        """Return a summary of all connected devices."""
        out: list[dict[str, Any]] = []
        for device_id, conn in self._conns.items():
            out.append({
                "device_id": device_id,
                "connected_at": conn.connected_at,
                "hello": conn.hello,
                "is_open": conn.is_open,
            })
        return out

    def __len__(self) -> int:
        return len(self._conns)

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._conns

    def __repr__(self) -> str:
        return f"<DeviceRegistry n={len(self._conns)}>"


class DeviceDisconnected(Exception):
    """Raised when pending futures are cancelled because the device disconnected."""
    pass
