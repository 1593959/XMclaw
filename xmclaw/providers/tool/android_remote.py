"""AndroidRemoteToolProvider — 把手机暴露成 agent 工具。

实现指南: ``docs/ANDROID_COMPANION_DEV_GUIDE_2026.md`` §3.3

每个 LLM 工具对应 §2.3 规范命令集中的一个下行命令；
invoke 里把工具参数打包成 ``{"ui":...}`` / ``{"clipboard_cmd":...}``
经 ``DeviceConn.send_request`` 下发。

安全:
  * 复用 ``security.tool_guard`` 对高风险动作（input/text 等）插入审批
  * 截图结果用 ``metadata.attach_image_url`` 让 agent 看到手机屏幕
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.daemon.device_registry import DeviceRegistry
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.security.device_redactor import DeviceRedactor
from xmclaw.utils.log import get_logger

log = get_logger(__name__)

# ------------------------------------------------------------------
# Key alias mapping (friendly → Android KEYCODE_*)
# ------------------------------------------------------------------
_KEY_ALIASES: dict[str, str] = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL",
    "del": "KEYCODE_DEL",
}

# ------------------------------------------------------------------
# ToolSpec definitions
# ------------------------------------------------------------------

_PHONE_OPEN_APP = ToolSpec(
    name="phone_open_app",
    description="Open an Android app by its package name.",
    parameters_schema={
        "type": "object",
        "properties": {
            "package_name": {"type": "string", "description": "Android package name, e.g. com.android.settings"},
            "device_id": {"type": "string", "description": "Optional device id; omit when only one phone is paired."},
        },
        "required": ["package_name"],
    },
    read_only=False,
)

_PHONE_CLICK = ToolSpec(
    name="phone_click",
    description="Click an element on the phone screen by its text, resource-id, or content-desc.",
    parameters_schema={
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "description": "Selector object: {text?, res_id?, desc?, index?, xpath?}",
            },
            "device_id": {"type": "string"},
        },
        "required": ["target"],
    },
    read_only=False,
)

_PHONE_TAP = ToolSpec(
    name="phone_tap",
    description="Tap a specific screen coordinate (x, y).",
    parameters_schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Screen x coordinate in pixels"},
            "y": {"type": "integer", "description": "Screen y coordinate in pixels"},
            "device_id": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    read_only=False,
)

_PHONE_INPUT = ToolSpec(
    name="phone_input",
    description="Type text into the currently focused editable field on the phone. Supports Chinese natively via AccessibilityNodeInfo.ACTION_SET_TEXT.",
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to input"},
            "index": {"type": "integer", "description": "Which editable field when multiple exist (default 0 = focused)", "default": 0},
            "device_id": {"type": "string"},
        },
        "required": ["text"],
    },
    read_only=False,
)

_PHONE_SWIPE = ToolSpec(
    name="phone_swipe",
    description="Swipe from (x1, y1) to (x2, y2) over ms milliseconds.",
    parameters_schema={
        "type": "object",
        "properties": {
            "x1": {"type": "integer"},
            "y1": {"type": "integer"},
            "x2": {"type": "integer"},
            "y2": {"type": "integer"},
            "ms": {"type": "integer", "description": "Duration in ms (default 300)", "default": 300},
            "device_id": {"type": "string"},
        },
        "required": ["x1", "y1", "x2", "y2"],
    },
    read_only=False,
)

_PHONE_KEY = ToolSpec(
    name="phone_key",
    description="Send a key event. Friendly names: back, home, recents, enter, delete. Also accepts KEYCODE_* constants.",
    parameters_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key name or KEYCODE_* constant"},
            "device_id": {"type": "string"},
        },
        "required": ["key"],
    },
    read_only=False,
)

_PHONE_SCREENSHOT = ToolSpec(
    name="phone_screenshot",
    description="Capture a screenshot of the phone screen and return it as an image attachment.",
    parameters_schema={
        "type": "object",
        "properties": {
            "device_id": {"type": "string"},
        },
    },
    read_only=True,
)

_PHONE_UI_TREE = ToolSpec(
    name="phone_ui_tree",
    description="Retrieve the accessibility UI tree of the phone screen as a flat list of nodes with text, bounds, and clickable info.",
    parameters_schema={
        "type": "object",
        "properties": {
            "clickable_only": {"type": "boolean", "description": "Only return clickable nodes", "default": False},
            "device_id": {"type": "string"},
        },
    },
    read_only=True,
)

_PHONE_NOTIFICATION = ToolSpec(
    name="phone_notification",
    description="Pull down the notification shade on the phone.",
    parameters_schema={
        "type": "object",
        "properties": {
            "device_id": {"type": "string"},
        },
    },
    read_only=False,
)

_PHONE_WAIT = ToolSpec(
    name="phone_wait",
    description="Wait until an element appears or disappears on the phone screen.",
    parameters_schema={
        "type": "object",
        "properties": {
            "event": {"type": "string", "enum": ["exists", "gone"], "default": "exists"},
            "target": {"type": "object", "description": "Selector object"},
            "timeout_ms": {"type": "integer", "default": 5000},
            "device_id": {"type": "string"},
        },
        "required": ["target"],
    },
    read_only=True,
)

_PHONE_CLIP_GET = ToolSpec(
    name="phone_clipboard_get",
    description="Read the phone clipboard contents.",
    parameters_schema={
        "type": "object",
        "properties": {
            "device_id": {"type": "string"},
        },
    },
    read_only=True,
)

_PHONE_CLIP_SET = ToolSpec(
    name="phone_clipboard_set",
    description="Write text to the phone clipboard.",
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "device_id": {"type": "string"},
        },
        "required": ["text"],
    },
    read_only=False,
)


# ------------------------------------------------------------------
# Provider
# ------------------------------------------------------------------

class AndroidRemoteToolProvider(ToolProvider):
    """Expose a connected Android phone as a set of agent tools."""

    def __init__(self, registry: DeviceRegistry, *, redactor: DeviceRedactor | None = None) -> None:
        self._reg = registry
        self._redactor = redactor

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------
    def list_tools(self) -> list[ToolSpec]:
        return [
            _PHONE_OPEN_APP,
            _PHONE_CLICK,
            _PHONE_TAP,
            _PHONE_INPUT,
            _PHONE_SWIPE,
            _PHONE_KEY,
            _PHONE_SCREENSHOT,
            _PHONE_UI_TREE,
            _PHONE_NOTIFICATION,
            _PHONE_WAIT,
            _PHONE_CLIP_GET,
            _PHONE_CLIP_SET,
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        a = call.args or {}
        conn = self._reg.get(a.get("device_id"))
        if conn is None:
            return _fail(call, "no paired phone connected (open the companion app and check connection)")

        cmd, kind = self._to_command(call.name, a)
        try:
            r = await conn.send_request("cmd", cmd)
        except asyncio.TimeoutError:
            return _fail(call, "device timeout: the phone did not respond within 15s")
        except Exception as exc:  # noqa: BLE001
            log.warning("android_remote_invoke_error", tool=call.name, exc=exc)
            return _fail(call, f"device connection error: {exc}")

        # Screenshot → attach image URL
        if kind == "image":
            url = r.get("url")
            return ToolResult(
                call_id=call.id,
                ok=True,
                content=json.dumps(r, ensure_ascii=False),
                metadata={"attach_image_url": url} if url else {},
            )

        # Tree → attach as structured content (PII redacted)
        if kind == "tree":
            if self._redactor is not None:
                if isinstance(r, dict):
                    raw_nodes = r.get("nodes", [])
                    if isinstance(raw_nodes, list):
                        r = {**r, "nodes": self._redactor.redact_tree(raw_nodes)}
                elif isinstance(r, list):
                    r = self._redactor.redact_tree(r)
            return _ok(call, json.dumps(r, ensure_ascii=False))

        # Act result with error
        if not r.get("ok", True) and "error" in r:
            return _fail(call, r["error"])

        return _ok(call, json.dumps(r, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Command mapping
    # ------------------------------------------------------------------
    def _to_command(self, name: str, a: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Map LLM tool name → Android command + expected response kind.

        Returns ``(cmd_dict, kind)`` where kind is one of:
        ``ack`` (act.result), ``image`` (obs.screenshot), ``tree`` (obs.tree).
        """
        if name == "phone_open_app":
            return {"ui": "open_app", "package_name": a["package_name"]}, "ack"
        if name == "phone_click":
            return {"ui": "click", "target": a["target"]}, "ack"
        if name == "phone_tap":
            return {"ui": "tap", "x": a["x"], "y": a["y"]}, "ack"
        if name == "phone_input":
            return {"ui": "input", "text": a["text"], "index": a.get("index", 0)}, "ack"
        if name == "phone_swipe":
            return {
                "ui": "swipe",
                "x1": a["x1"], "y1": a["y1"],
                "x2": a["x2"], "y2": a["y2"],
                "ms": a.get("ms", 300),
            }, "ack"
        if name == "phone_key":
            key = str(a["key"]).lower()
            return {"ui": "key_event", "key": _KEY_ALIASES.get(key, a["key"])}, "ack"
        if name == "phone_screenshot":
            return {"ui": "screenshot"}, "image"
        if name == "phone_ui_tree":
            return {"ui": "tree", "clickable_only": a.get("clickable_only", False)}, "tree"
        if name == "phone_notification":
            return {"ui": "notification"}, "ack"
        if name == "phone_wait":
            return {
                "ui": "wait",
                "event": a.get("event", "exists"),
                "target": a["target"],
                "timeout_ms": a.get("timeout_ms", 5000),
            }, "ack"
        if name == "phone_clipboard_get":
            return {"clipboard_cmd": "get_clipboard"}, "ack"
        if name == "phone_clipboard_set":
            return {"clipboard_cmd": "set_clipboard", "text": a["text"]}, "ack"
        raise ValueError(f"unknown android tool: {name}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _fail(call: ToolCall, msg: str) -> ToolResult:
    return ToolResult(call_id=call.id, ok=False, content=msg, error=msg)


def _ok(call: ToolCall, content: str) -> ToolResult:
    return ToolResult(call_id=call.id, ok=True, content=content)
