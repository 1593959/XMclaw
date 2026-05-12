"""ComputerUseTools — give the agent a mouse, a keyboard, and eyes.

2026-05-12. Pre-this the agent could only act through the shell + file
tools + browser (playwright). Anything that lived in a GUI app
(Photoshop, native desktop apps, OS dialogs, anything Chrome doesn't
own) was unreachable. The agent's only option was "tell the user to
click X" — which defeats the whole "personal Jarvis" thesis.

Tool set
========

Vision:
  * ``screen_capture``  — full-screen PNG, returns path + base64 + size
  * ``screen_size``     — viewport dimensions (no capture)
  * ``cursor_position`` — current mouse (x, y)

Mouse:
  * ``mouse_move``      — to (x, y), optional duration for smooth motion
  * ``mouse_click``     — left/right/middle, single/double, at coords
  * ``mouse_drag``      — press → move → release (for drag-and-drop)
  * ``mouse_scroll``    — vertical wheel, +/- clicks

Keyboard:
  * ``keyboard_type``   — literal string (uses keyboard layout)
  * ``keyboard_press``  — single key or chord (``"ctrl+c"``, ``"alt+f4"``,
                          ``"win+r"``). Names follow pyautogui's spec.

Windows (cross-platform):
  * ``window_list``     — title + bbox of every visible top-level window
  * ``window_focus``    — bring a window to front by partial title match

Safety
======

This is the most dangerous tool surface XMclaw exposes — the agent
literally drives the user's GUI. Three independent gates:

1. **Provider-level off by default.** ``tools.computer_use.enabled``
   in ``daemon/config.json`` must be ``true``. With it false, this
   module's ``ComputerUseTools`` isn't even constructed — its tools
   never appear in ``list_tools()``, so the LLM can't see them.

2. **Tool-category mapping**:
   ``computer_use → DANGEROUS`` in ``xmclaw/utils/security.py:71``
   maps to ``PermissionLevel.BLOCK`` by default. With Guardians
   wired (``security.guardians.enabled=true``), each invocation
   hits a confirmation gate before the action runs.

3. **pyautogui FAILSAFE.** ``pyautogui.FAILSAFE = True`` (the
   library's default) — moving the cursor to a screen corner
   aborts the program with ``FailSafeException``. We keep this on
   so the user can always slam the mouse top-left to interrupt
   a runaway agent.

Per-tool latency cap: 30s (mouse moves with ``duration > 30`` get
clipped). Network is irrelevant — every call is OS-local.

Missing deps degrade gracefully: every tool that needs ``pyautogui``
returns ``ToolResult(ok=False, error="pyautogui not installed")``
instead of crashing the daemon.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import time
from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# ── Defaults / caps ───────────────────────────────────────────────────


_DEFAULT_SCREENSHOT_DIR = "screenshots"  # under data_dir / v2
_MAX_DURATION_S = 30.0
_MAX_TYPE_LEN = 4000
_VALID_BUTTONS = {"left", "right", "middle"}
_MAX_WINDOWS_RETURNED = 60


# ── Specs ─────────────────────────────────────────────────────────────


_SCREEN_CAPTURE_SPEC = ToolSpec(
    name="screen_capture",
    description=(
        "Take a full-screen screenshot. Returns "
        "{path, size: [w, h], base64_png (truncated to ~512KB), "
        "monitor_index}. Default captures the primary monitor "
        "(index 1 in mss). Pass ``monitor`` to pick a specific "
        "monitor (0 = virtual screen union of all monitors).\n\n"
        "Use the returned ``path`` for follow-up vision processing; "
        "``base64_png`` lets the LLM see what's on screen in the same "
        "turn when an image-aware model is in use. The base64 stream "
        "is capped — for raw access read the file."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "monitor": {
                "type": "integer",
                "description": "mss monitor index (default 1 = primary).",
            },
            "include_base64": {
                "type": "boolean",
                "description": "Return base64 inline. Default true.",
            },
        },
    },
)

_SCREEN_SIZE_SPEC = ToolSpec(
    name="screen_size",
    description=(
        "Return {width, height} of the primary monitor without "
        "capturing pixels. Use this when planning a click — you need "
        "the bounds before you can reason about percentages or edges."
    ),
    parameters_schema={"type": "object", "properties": {}},
)

_CURSOR_POSITION_SPEC = ToolSpec(
    name="cursor_position",
    description="Return {x, y} of the current mouse cursor.",
    parameters_schema={"type": "object", "properties": {}},
)

_MOUSE_MOVE_SPEC = ToolSpec(
    name="mouse_move",
    description=(
        "Move the cursor to (x, y). ``duration`` (seconds, 0-30) "
        "controls easing — 0 = instant (snap), >0 = smooth pyautogui "
        "tween. Use a small duration (0.2-0.5s) when the LLM is "
        "watching the screenshots streamed back; users see the cursor "
        "move and don't panic."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "duration": {"type": "number", "description": "0-30s, default 0.25"},
        },
        "required": ["x", "y"],
    },
)

_MOUSE_CLICK_SPEC = ToolSpec(
    name="mouse_click",
    description=(
        "Click at (x, y) or at the current cursor position when x/y "
        "are omitted. ``button`` ∈ {left, right, middle}, default "
        "left. ``count`` 1-3, default 1 (2 = double-click). Always "
        "moves to the target first (instant) before clicking — "
        "guarantees the click lands on the intended pixel."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "count": {"type": "integer", "description": "1-3"},
        },
    },
)

_MOUSE_DRAG_SPEC = ToolSpec(
    name="mouse_drag",
    description=(
        "Drag from (start_x, start_y) to (end_x, end_y). Used for "
        "drag-and-drop, range selection, slider adjustment. "
        "``button`` defaults to left. ``duration`` defaults to 0.5s "
        "(some apps reject too-fast drags as accidental)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "start_x": {"type": "integer"},
            "start_y": {"type": "integer"},
            "end_x":   {"type": "integer"},
            "end_y":   {"type": "integer"},
            "button":  {"type": "string", "enum": ["left", "right", "middle"]},
            "duration": {"type": "number"},
        },
        "required": ["start_x", "start_y", "end_x", "end_y"],
    },
)

_MOUSE_SCROLL_SPEC = ToolSpec(
    name="mouse_scroll",
    description=(
        "Vertical scroll at (x, y) or current cursor position. "
        "``clicks`` positive = up, negative = down. Each click ≈ "
        "one wheel notch (≈ 3 lines in most apps)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "clicks": {"type": "integer"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        },
        "required": ["clicks"],
    },
)

_KEYBOARD_TYPE_SPEC = ToolSpec(
    name="keyboard_type",
    description=(
        "Type literal text using the OS keyboard layout. Capped at "
        "4000 chars per call — break long inserts into multiple "
        "calls. ``interval`` (seconds between keystrokes, default 0) "
        "helps with apps that drop characters when typed too fast.\n\n"
        "Does NOT press Enter — call ``keyboard_press 'enter'`` "
        "explicitly when you want a submit."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "interval": {"type": "number", "description": "0-1s between keys."},
        },
        "required": ["text"],
    },
)

_KEYBOARD_PRESS_SPEC = ToolSpec(
    name="keyboard_press",
    description=(
        "Press a single key or a chord. Examples:\n"
        "  ``\"enter\"`` / ``\"esc\"`` / ``\"tab\"`` / ``\"backspace\"``\n"
        "  ``\"ctrl+c\"`` / ``\"ctrl+shift+t\"`` / ``\"alt+tab\"``\n"
        "  ``\"win+r\"`` (Windows Run dialog) / ``\"cmd+space\"`` (macOS)\n"
        "  ``\"f5\"`` / ``\"pageup\"`` / ``\"home\"`` / ``\"end\"``\n\n"
        "Key names follow pyautogui's spec — see "
        "``pyautogui.KEYBOARD_KEYS`` for the full table. Chords use "
        "``+`` as separator (no spaces). Plain ``\"a\"`` types a "
        "lowercase a; for uppercase use ``\"shift+a\"`` or use "
        "``keyboard_type`` for arbitrary text."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"keys": {"type": "string"}},
        "required": ["keys"],
    },
)

_WINDOW_LIST_SPEC = ToolSpec(
    name="window_list",
    description=(
        "List visible top-level windows: {title, bbox: [x, y, w, h], "
        "is_minimized, is_active}. Capped at 60 entries. Cross-"
        "platform via ``pygetwindow``; on Linux falls back to "
        "``wmctrl`` when pygetwindow's X11 backend isn't available."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "title_contains": {
                "type": "string",
                "description": "Optional substring filter (case-insensitive).",
            },
        },
    },
)

_WINDOW_FOCUS_SPEC = ToolSpec(
    name="window_focus",
    description=(
        "Bring a window to the foreground by partial title match. "
        "When multiple windows match, picks the first non-minimized "
        "one. Returns the matched title + bbox after activation. "
        "Use ``window_list`` first to disambiguate."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"title_contains": {"type": "string"}},
        "required": ["title_contains"],
    },
)


# ── 2026-05-12 vision-grounding specs ─────────────────────────────────


_SCREEN_OCR_SPEC = ToolSpec(
    name="screen_ocr",
    description=(
        "OCR the current screen (or a region) and return text blocks "
        "with bounding boxes. Each block: {text, bbox: [x, y, w, h], "
        "center: [cx, cy], confidence}. Pair with ``mouse_click`` to "
        "click on detected text without pixel-perfect coordinate "
        "guessing.\n\n"
        "Optional ``region`` clips the OCR area before running — "
        "faster + more accurate for small targets (e.g. searching "
        "only the chat-list panel of WeChat). Format: [x, y, w, h].\n\n"
        "Needs an OCR engine — tries in order: rapidocr-onnxruntime "
        "(Chinese-friendly, ~50MB) → paddleocr → pytesseract. Each "
        "is an optional pip install; the tool returns an install "
        "hint when none are available."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "region": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "[x, y, w, h] clipping rectangle.",
            },
            "min_confidence": {
                "type": "number",
                "description": "Drop blocks below this. Default 0.5.",
            },
        },
    },
)

_FIND_ON_SCREEN_SPEC = ToolSpec(
    name="find_on_screen",
    description=(
        "Locate text on the current screen and return its center "
        "coordinates + bbox. Returns {found, x, y, bbox, "
        "match_text, confidence, all_matches: [...]}. Matching is "
        "case-insensitive + substring; pass ``exact: true`` to "
        "require full-cell match.\n\n"
        "Multiple matches → returns the highest-confidence one; "
        "``all_matches`` lists the rest so the LLM can disambiguate. "
        "Use ``region`` to scope (same shape as screen_ocr)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "region": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "exact": {"type": "boolean"},
            "min_confidence": {"type": "number"},
        },
        "required": ["text"],
    },
)

_CLICK_ON_TEXT_SPEC = ToolSpec(
    name="click_on_text",
    description=(
        "One-shot: find text on screen + move + click its center. "
        "Returns the match metadata from find_on_screen + the click "
        "coordinates. ``button`` ∈ {left, right, middle}, ``count`` "
        "1-3 (1=single, 2=double). Wraps the find + click sequence "
        "so the LLM can write 'click the 魔丸 group in the chat "
        "list' as one tool call instead of three.\n\n"
        "Returns ok=False with the OCR matches list when the text "
        "isn't found — the LLM can adjust its query and retry."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text":   {"type": "string"},
            "region": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "count":  {"type": "integer", "description": "1-3"},
            "exact":  {"type": "boolean"},
            "min_confidence": {"type": "number"},
        },
        "required": ["text"],
    },
)

_WAIT_FOR_TEXT_SPEC = ToolSpec(
    name="wait_for_text",
    description=(
        "Poll the screen with OCR until ``text`` appears (or "
        "``timeout_s`` elapses). Returns find_on_screen's result "
        "when found, or {found: false} on timeout. Use for waiting "
        "on UI elements that load after a click (e.g. 'open WeChat "
        "then wait for the chat list to render')."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "timeout_s": {
                "type": "number",
                "description": "0.5-30, default 5.",
            },
            "poll_interval_s": {
                "type": "number",
                "description": "0.2-5, default 0.6.",
            },
            "region": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "exact": {"type": "boolean"},
        },
        "required": ["text"],
    },
)

_REGION_CAPTURE_SPEC = ToolSpec(
    name="screen_region_capture",
    description=(
        "Capture a rectangular region of the screen → JPG (lighter "
        "than full PNG for cropped vision-LLM input). Returns "
        "{path, region, base64_jpg}. Useful when you've already "
        "OCR'd and want to send the LLM only the relevant pane."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "region": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "[x, y, w, h]",
            },
            "include_base64": {"type": "boolean"},
            "quality": {
                "type": "integer",
                "description": "JPEG quality 1-100, default 85.",
            },
        },
        "required": ["region"],
    },
)


# ── 2026-05-12 (round 2): image template + scroll + native UI ────────


_FIND_IMAGE_SPEC = ToolSpec(
    name="find_image_on_screen",
    description=(
        "Locate a template image on the current screen. Use for "
        "icon-based UI elements that OCR can't read (send buttons, "
        "settings gears, app icons in the taskbar).\n\n"
        "Workflow:\n"
        "  1. screen_region_capture the icon once → save as template\n"
        "  2. find_image_on_screen with that template path → "
        "     returns {found, x, y, bbox, confidence}\n\n"
        "``confidence`` 0-1 controls match strictness (default 0.8, "
        "lower for slight scaling / antialiasing tolerance). "
        "``region`` clips the search area. Needs ``opencv-python``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "template_path": {"type": "string"},
            "confidence": {
                "type": "number",
                "description": "0-1, default 0.8.",
            },
            "region": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "[x, y, w, h] search clip.",
            },
        },
        "required": ["template_path"],
    },
)

_CLICK_IMAGE_SPEC = ToolSpec(
    name="click_on_image",
    description=(
        "Convenience: find_image_on_screen + mouse_click in one. "
        "Returns the find result + click coords. Same args as "
        "find_image_on_screen plus button / count."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "template_path": {"type": "string"},
            "confidence":    {"type": "number"},
            "region":        {"type": "array", "items": {"type": "integer"}},
            "button":        {"type": "string", "enum": ["left", "right", "middle"]},
            "count":         {"type": "integer", "description": "1-3"},
        },
        "required": ["template_path"],
    },
)

_SCROLL_TO_TEXT_SPEC = ToolSpec(
    name="scroll_to_text",
    description=(
        "Scroll at (x, y) until ``text`` appears on screen, or give "
        "up after ``max_scrolls`` attempts. Use for long lists "
        "(group lists, settings menus, search results).\n\n"
        "After each scroll, re-OCRs the (optional) ``region`` and "
        "checks if the text is visible. ``direction`` ∈ {down, up}, "
        "default down. Returns the same shape as find_on_screen "
        "when found, or {found: false, scrolls_tried} on giveup."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "x": {"type": "integer", "description": "Scroll-anchor x."},
            "y": {"type": "integer", "description": "Scroll-anchor y."},
            "direction": {"type": "string", "enum": ["down", "up"]},
            "max_scrolls": {"type": "integer", "description": "1-30, default 10."},
            "scroll_amount": {
                "type": "integer",
                "description": "Wheel notches per scroll, default 3.",
            },
            "region": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "exact": {"type": "boolean"},
        },
        "required": ["text"],
    },
)

_UI_INSPECT_SPEC = ToolSpec(
    name="ui_inspect",
    description=(
        "Read the accessibility tree of the currently focused "
        "Windows window — every button / textbox / list / etc. "
        "shows up with its name + automation_id + bbox. WAY more "
        "reliable than OCR for native apps (WeChat, QQ, Office, "
        "Explorer) because the OS exposes the structured UI "
        "directly.\n\n"
        "Filter with ``control_type`` (e.g. 'Button', 'Edit', "
        "'List') and/or ``name_contains`` to scope. Capped at 100 "
        "elements per call.\n\n"
        "Windows-only (uses ``uiautomation`` package). Returns "
        "ok=False with install hint elsewhere — Linux/macOS will "
        "need AT-SPI / AXUIElement backends in a future pass."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "control_type": {
                "type": "string",
                "description": "e.g. Button / Edit / List / Text / "
                               "Pane. Omit to return all.",
            },
            "name_contains": {
                "type": "string",
                "description": "Substring filter on the element's name.",
            },
            "window_title": {
                "type": "string",
                "description": "Target window title contains. Omit "
                               "to use the foreground window.",
            },
            "max_depth": {
                "type": "integer",
                "description": "Tree walk depth (1-12, default 6).",
            },
        },
    },
)

_UI_CLICK_SPEC = ToolSpec(
    name="ui_click",
    description=(
        "Click an element by accessibility name (or automation_id) "
        "via the Windows UIAutomation API. Much more reliable than "
        "OCR-then-coords for native apps: it works even when the "
        "button is rendered as just an icon, or the text is anti-"
        "aliased weird, or the window scrolls. The element doesn't "
        "have to be visible — UIAutomation can also invoke pattern.\n\n"
        "Search order: name_contains exact → name_contains substring "
        "→ automation_id exact. Returns the matched element's name + "
        "bbox after invoke. Windows-only."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name_contains": {"type": "string"},
            "automation_id": {"type": "string"},
            "control_type":  {"type": "string"},
            "window_title":  {"type": "string"},
            "double_click":  {"type": "boolean"},
        },
    },
)


# ── Provider ──────────────────────────────────────────────────────────


class ComputerUseTools(ToolProvider):
    """Mouse + keyboard + window control for desktop automation.

    Constructor params:

    * ``screenshot_dir`` — where ``screen_capture`` writes PNGs.
      Default = ``<data_dir>/v2/screenshots``.
    * ``base64_size_cap`` — max bytes returned inline as base64.
      Default 512 KB. Larger captures are still saved to disk; the
      ``base64_png`` field is dropped from the response.

    Tools degrade individually when their dep is missing — the
    provider always lists them so the LLM can see what's *possible*
    even when one route is broken. Failures surface as
    ``ToolResult(ok=False, error="...")`` with an install hint.
    """

    def __init__(
        self,
        *,
        screenshot_dir: str | Path | None = None,
        base64_size_cap: int = 512 * 1024,
    ) -> None:
        if screenshot_dir is None:
            from xmclaw.utils.paths import data_dir
            screenshot_dir = data_dir() / "v2" / _DEFAULT_SCREENSHOT_DIR
        self._screenshot_dir = Path(screenshot_dir)
        self._base64_size_cap = int(base64_size_cap)
        # Initialise pyautogui's FAILSAFE on first import — done lazily
        # in _require_pyautogui so a daemon that never invokes a tool
        # doesn't pay the import cost.
        self._pyautogui_ready: bool | None = None

    def list_tools(self) -> list[ToolSpec]:
        return [
            _SCREEN_CAPTURE_SPEC, _SCREEN_SIZE_SPEC, _CURSOR_POSITION_SPEC,
            _MOUSE_MOVE_SPEC, _MOUSE_CLICK_SPEC, _MOUSE_DRAG_SPEC,
            _MOUSE_SCROLL_SPEC,
            _KEYBOARD_TYPE_SPEC, _KEYBOARD_PRESS_SPEC,
            _WINDOW_LIST_SPEC, _WINDOW_FOCUS_SPEC,
            # Vision-grounding (2026-05-12)
            _SCREEN_OCR_SPEC, _FIND_ON_SCREEN_SPEC,
            _CLICK_ON_TEXT_SPEC, _WAIT_FOR_TEXT_SPEC,
            _REGION_CAPTURE_SPEC,
            # Image template + scroll + native Windows UIA (2026-05-12 r2)
            _FIND_IMAGE_SPEC, _CLICK_IMAGE_SPEC, _SCROLL_TO_TEXT_SPEC,
            _UI_INSPECT_SPEC, _UI_CLICK_SPEC,
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        name = call.name
        args = call.args or {}
        try:
            if name == "screen_capture":   return await self._screen_capture(call, t0, args)
            if name == "screen_size":      return await self._screen_size(call, t0)
            if name == "cursor_position":  return await self._cursor_position(call, t0)
            if name == "mouse_move":       return await self._mouse_move(call, t0, args)
            if name == "mouse_click":      return await self._mouse_click(call, t0, args)
            if name == "mouse_drag":       return await self._mouse_drag(call, t0, args)
            if name == "mouse_scroll":     return await self._mouse_scroll(call, t0, args)
            if name == "keyboard_type":    return await self._keyboard_type(call, t0, args)
            if name == "keyboard_press":   return await self._keyboard_press(call, t0, args)
            if name == "window_list":      return await self._window_list(call, t0, args)
            if name == "window_focus":     return await self._window_focus(call, t0, args)
            # 2026-05-12 vision-grounding
            if name == "screen_ocr":            return await self._screen_ocr(call, t0, args)
            if name == "find_on_screen":        return await self._find_on_screen(call, t0, args)
            if name == "click_on_text":         return await self._click_on_text(call, t0, args)
            if name == "wait_for_text":         return await self._wait_for_text(call, t0, args)
            if name == "screen_region_capture": return await self._screen_region_capture(call, t0, args)
            # 2026-05-12 r2: image template + scroll + native UIA
            if name == "find_image_on_screen":  return await self._find_image_on_screen(call, t0, args)
            if name == "click_on_image":        return await self._click_on_image(call, t0, args)
            if name == "scroll_to_text":        return await self._scroll_to_text(call, t0, args)
            if name == "ui_inspect":            return await self._ui_inspect(call, t0, args)
            if name == "ui_click":              return await self._ui_click(call, t0, args)
        except Exception as exc:  # noqa: BLE001 — surface as ok=False
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")
        return _fail(call, t0, f"unknown tool: {name!r}")

    # ── pyautogui import gate ──────────────────────────────────────

    def _require_pyautogui(self):
        """Return the pyautogui module or raise ImportError with a
        clear install hint. Called from every mouse/keyboard tool."""
        import pyautogui
        if self._pyautogui_ready is None:
            # FAILSAFE: cursor at (0,0) aborts. User can always escape
            # a misbehaving agent by slamming the mouse top-left.
            pyautogui.FAILSAFE = True
            # PAUSE: pyautogui sleeps this many seconds after each
            # call. Set small (0.05s) — enough to let GUI update,
            # short enough to feel responsive.
            pyautogui.PAUSE = 0.05
            self._pyautogui_ready = True
        return pyautogui

    # ── Vision ─────────────────────────────────────────────────────

    async def _screen_capture(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            import mss
        except ImportError:
            return _fail(call, t0, "screen_capture needs ``mss``. pip install mss")
        monitor_idx = int(args.get("monitor", 1))
        # B-Vision: default is now NO base64 in the tool result text.
        # Instead we set ``metadata.attach_image`` so hop_loop injects
        # the screenshot as a real vision content block on the NEXT
        # user message — the model literally SEES the screen instead
        # of OCRing it. ``include_base64=true`` opt-in still works for
        # legacy callers but is almost always wrong (1+ MB text in
        # the tool result, model can't read base64 from text anyway).
        include_b64 = bool(args.get("include_base64", False))

        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}_{call.id[:8]}.png"
        out = self._screenshot_dir / fname

        def _do_capture() -> tuple[int, int]:
            with mss.mss() as sct:
                if not (0 <= monitor_idx < len(sct.monitors)):
                    raise ValueError(
                        f"monitor index {monitor_idx} out of range "
                        f"(have {len(sct.monitors)} monitors; 0=virtual, "
                        "1=primary)",
                    )
                mon = sct.monitors[monitor_idx]
                grab = sct.grab(mon)
                mss.tools.to_png(grab.rgb, grab.size, output=str(out))
                return grab.size

        try:
            size = await asyncio.to_thread(_do_capture)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"mss capture failed: {exc}")

        result: dict[str, Any] = {
            "path": str(out),
            "size": [int(size[0]), int(size[1])],
            "monitor_index": monitor_idx,
            "vision_attached": True,
        }
        if include_b64:
            try:
                raw = out.read_bytes()
                if len(raw) <= self._base64_size_cap:
                    result["base64_png"] = base64.b64encode(raw).decode("ascii")
                else:
                    result["base64_omitted"] = (
                        f"{len(raw)} bytes > cap {self._base64_size_cap}; "
                        "read the file from `path` instead"
                    )
            except OSError as exc:
                result["base64_omitted"] = f"read failed: {exc}"

        return ToolResult(
            call_id=call.id, ok=True,
            content=json.dumps(result, ensure_ascii=False),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata={"attach_image": str(out)},
        )

    async def _screen_size(self, call: ToolCall, t0: float) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        size = pg.size()
        return _ok(call, t0, json.dumps({"width": int(size[0]), "height": int(size[1])}))

    async def _cursor_position(self, call: ToolCall, t0: float) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        pos = pg.position()
        return _ok(call, t0, json.dumps({"x": int(pos[0]), "y": int(pos[1])}))

    # ── Mouse ──────────────────────────────────────────────────────

    async def _mouse_move(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        try:
            x = int(args["x"]); y = int(args["y"])
        except (KeyError, TypeError, ValueError):
            return _fail(call, t0, "x, y (int) required")
        duration = _clamp(float(args.get("duration", 0.25)), 0.0, _MAX_DURATION_S)
        await asyncio.to_thread(pg.moveTo, x, y, duration=duration)
        return _ok(call, t0, json.dumps({"x": x, "y": y, "duration": duration}))

    async def _mouse_click(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        button = str(args.get("button", "left"))
        if button not in _VALID_BUTTONS:
            return _fail(call, t0, f"button must be one of {sorted(_VALID_BUTTONS)}")
        count = _clamp(int(args.get("count", 1)), 1, 3)
        x = args.get("x"); y = args.get("y")
        if x is not None and y is not None:
            try:
                x = int(x); y = int(y)
            except (TypeError, ValueError):
                return _fail(call, t0, "x/y must be ints when provided")
            await asyncio.to_thread(
                pg.click, x=x, y=y, button=button, clicks=count,
            )
            return _ok(call, t0, json.dumps({
                "x": x, "y": y, "button": button, "count": count,
            }))
        await asyncio.to_thread(pg.click, button=button, clicks=count)
        pos = pg.position()
        return _ok(call, t0, json.dumps({
            "x": int(pos[0]), "y": int(pos[1]),
            "button": button, "count": count,
        }))

    async def _mouse_drag(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        try:
            sx = int(args["start_x"]); sy = int(args["start_y"])
            ex = int(args["end_x"]); ey = int(args["end_y"])
        except (KeyError, TypeError, ValueError):
            return _fail(
                call, t0,
                "start_x, start_y, end_x, end_y (int) required",
            )
        button = str(args.get("button", "left"))
        if button not in _VALID_BUTTONS:
            return _fail(call, t0, f"button must be one of {sorted(_VALID_BUTTONS)}")
        duration = _clamp(float(args.get("duration", 0.5)), 0.0, _MAX_DURATION_S)

        def _drag():
            pg.moveTo(sx, sy, duration=0)
            pg.dragTo(ex, ey, duration=duration, button=button)

        await asyncio.to_thread(_drag)
        return _ok(call, t0, json.dumps({
            "from": [sx, sy], "to": [ex, ey],
            "button": button, "duration": duration,
        }))

    async def _mouse_scroll(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        try:
            clicks = int(args["clicks"])
        except (KeyError, TypeError, ValueError):
            return _fail(call, t0, "clicks (int) required (negative = down)")
        x = args.get("x"); y = args.get("y")
        if x is not None and y is not None:
            try:
                x = int(x); y = int(y)
            except (TypeError, ValueError):
                return _fail(call, t0, "x/y must be ints when provided")
            await asyncio.to_thread(pg.scroll, clicks, x=x, y=y)
        else:
            await asyncio.to_thread(pg.scroll, clicks)
        return _ok(call, t0, json.dumps({
            "clicks": clicks, "x": x, "y": y,
        }))

    # ── Keyboard ───────────────────────────────────────────────────

    async def _keyboard_type(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        text = args.get("text")
        if not isinstance(text, str):
            return _fail(call, t0, "text (string) required")
        if len(text) > _MAX_TYPE_LEN:
            return _fail(
                call, t0,
                f"text > {_MAX_TYPE_LEN} chars — split into multiple "
                "keyboard_type calls",
            )
        interval = _clamp(float(args.get("interval", 0.0)), 0.0, 1.0)
        await asyncio.to_thread(pg.write, text, interval=interval)
        return _ok(call, t0, json.dumps({
            "chars": len(text), "interval": interval,
        }))

    async def _keyboard_press(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        keys = args.get("keys")
        if not isinstance(keys, str) or not keys.strip():
            return _fail(call, t0, "keys (string) required")
        keys = keys.strip().lower()
        # Chord: "ctrl+shift+t" → hotkey('ctrl', 'shift', 't')
        if "+" in keys:
            parts = [p.strip() for p in keys.split("+") if p.strip()]
            await asyncio.to_thread(pg.hotkey, *parts)
            return _ok(call, t0, json.dumps({
                "kind": "chord", "keys": parts,
            }))
        await asyncio.to_thread(pg.press, keys)
        return _ok(call, t0, json.dumps({
            "kind": "press", "key": keys,
        }))

    # ── Windows ────────────────────────────────────────────────────

    async def _window_list(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            import pygetwindow as gw  # type: ignore
        except ImportError:
            # On non-Windows Linux without an X11 server pygetwindow
            # raises at import-time; same install hint applies.
            return _fail(
                call, t0,
                "window_list needs ``pygetwindow``. "
                "pip install pygetwindow (Windows / macOS / X11 Linux)",
            )
        substring = str(args.get("title_contains", "")).strip().lower()
        try:
            windows = await asyncio.to_thread(gw.getAllWindows)
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"pygetwindow.getAllWindows failed: {exc}")
        out: list[dict[str, Any]] = []
        for w in windows:
            try:
                title = (getattr(w, "title", "") or "").strip()
                if not title:
                    continue
                if substring and substring not in title.lower():
                    continue
                out.append({
                    "title": title[:160],
                    "bbox": [
                        int(getattr(w, "left", 0)),
                        int(getattr(w, "top", 0)),
                        int(getattr(w, "width", 0)),
                        int(getattr(w, "height", 0)),
                    ],
                    "is_minimized": bool(getattr(w, "isMinimized", False)),
                    "is_active":    bool(getattr(w, "isActive", False)),
                })
            except Exception:  # noqa: BLE001 — pygetwindow attrs vary by OS
                continue
            if len(out) >= _MAX_WINDOWS_RETURNED:
                break
        return _ok(call, t0, json.dumps({
            "count": len(out), "windows": out,
            "platform": platform.system(),
        }, ensure_ascii=False))

    async def _window_focus(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        try:
            import pygetwindow as gw  # type: ignore
        except ImportError:
            return _fail(
                call, t0,
                "window_focus needs ``pygetwindow``. "
                "pip install pygetwindow",
            )
        substring = str(args.get("title_contains", "")).strip()
        if not substring:
            return _fail(call, t0, "title_contains required")

        def _do_focus() -> dict[str, Any]:
            candidates = [
                w for w in gw.getAllWindows()
                if (getattr(w, "title", "") or "").strip()
                and substring.lower() in (w.title or "").lower()
            ]
            if not candidates:
                raise LookupError(
                    f"no visible window with title containing {substring!r}",
                )
            # Prefer non-minimized; fall back to first match
            for w in candidates:
                if not getattr(w, "isMinimized", False):
                    chosen = w
                    break
            else:
                chosen = candidates[0]
                # restore() may not exist on every platform — best effort
                try:
                    chosen.restore()
                except Exception:  # noqa: BLE001
                    pass
            try:
                chosen.activate()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"activate failed for {chosen.title!r}: {exc}",
                )
            return {
                "title": (chosen.title or "")[:160],
                "bbox": [
                    int(getattr(chosen, "left", 0)),
                    int(getattr(chosen, "top", 0)),
                    int(getattr(chosen, "width", 0)),
                    int(getattr(chosen, "height", 0)),
                ],
            }

        try:
            payload = await asyncio.to_thread(_do_focus)
        except (LookupError, RuntimeError) as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"window_focus failed: {exc}")
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

    # ── 2026-05-12 vision-grounding ────────────────────────────────

    async def _screen_ocr(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        region = args.get("region")
        min_conf = float(args.get("min_confidence", 0.5))
        try:
            blocks = await asyncio.to_thread(
                _run_ocr_full_pipeline, region, min_conf,
            )
        except _NoOCREngineError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"screen_ocr failed: {type(exc).__name__}: {exc}",
            )
        return _ok(call, t0, json.dumps({
            "blocks": blocks,
            "count": len(blocks),
            "region": region,
        }, ensure_ascii=False))

    async def _find_on_screen(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(call, t0, "text (non-empty string) required")
        region = args.get("region")
        exact = bool(args.get("exact", False))
        min_conf = float(args.get("min_confidence", 0.5))

        try:
            blocks = await asyncio.to_thread(
                _run_ocr_full_pipeline, region, min_conf,
            )
        except _NoOCREngineError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, t0, f"OCR failed: {exc}")

        matches = _match_text_in_blocks(blocks, text, exact=exact)
        if not matches:
            # Surface the top blocks so the LLM can see what WAS read
            # and adjust the query. This is more useful than a blank
            # "not found" — the LLM can spot "I asked for '魔丸群' but
            # the OCR read '魔丸' (cut off)".
            return _fail(
                call, t0,
                json.dumps({
                    "found": False,
                    "wanted": text,
                    "matched_zero": True,
                    "sample_blocks": [
                        {"text": b["text"], "confidence": b["confidence"]}
                        for b in blocks[:20]
                    ],
                }, ensure_ascii=False),
            )
        best = matches[0]
        return _ok(call, t0, json.dumps({
            "found": True,
            "x": best["center"][0],
            "y": best["center"][1],
            "bbox": best["bbox"],
            "match_text": best["text"],
            "confidence": best["confidence"],
            "all_matches": matches[1:5],  # top 4 alternatives
        }, ensure_ascii=False))

    async def _click_on_text(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        # Step 1: find
        find_args = {
            k: v for k, v in args.items()
            if k in ("text", "region", "exact", "min_confidence")
        }
        find_call = ToolCall(
            id=call.id + "-find",
            name="find_on_screen",
            args=find_args,
            provenance=call.provenance,
            session_id=call.session_id,
        )
        find_result = await self._find_on_screen(find_call, t0, find_args)
        if not find_result.ok:
            # Bubble up the same diagnostic shape (with sample_blocks)
            # so the LLM can adjust its query.
            return _fail(call, t0, find_result.error)

        find_payload = json.loads(find_result.content)
        x, y = find_payload["x"], find_payload["y"]

        # Step 2: click
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        button = str(args.get("button", "left"))
        if button not in _VALID_BUTTONS:
            return _fail(
                call, t0,
                f"button must be one of {sorted(_VALID_BUTTONS)}",
            )
        count = _clamp(int(args.get("count", 1)), 1, 3)
        try:
            await asyncio.to_thread(
                pg.click, x=x, y=y, button=button, clicks=count,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"click at ({x},{y}) failed: {type(exc).__name__}: {exc}",
            )

        return _ok(call, t0, json.dumps({
            "clicked": True,
            "x": x, "y": y,
            "button": button,
            "count": count,
            "match_text": find_payload["match_text"],
            "confidence": find_payload["confidence"],
            "bbox": find_payload["bbox"],
        }, ensure_ascii=False))

    async def _wait_for_text(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(call, t0, "text required")
        timeout_s = _clamp(float(args.get("timeout_s", 5.0)), 0.5, 30.0)
        poll = _clamp(float(args.get("poll_interval_s", 0.6)), 0.2, 5.0)
        region = args.get("region")
        exact = bool(args.get("exact", False))

        deadline = time.perf_counter() + timeout_s
        attempts = 0
        last_blocks: list = []
        while time.perf_counter() < deadline:
            attempts += 1
            try:
                blocks = await asyncio.to_thread(
                    _run_ocr_full_pipeline, region, 0.5,
                )
                last_blocks = blocks
            except _NoOCREngineError as exc:
                return _fail(call, t0, str(exc))
            except Exception:  # noqa: BLE001
                blocks = []
            matches = _match_text_in_blocks(blocks, text, exact=exact)
            if matches:
                best = matches[0]
                return _ok(call, t0, json.dumps({
                    "found": True,
                    "x": best["center"][0],
                    "y": best["center"][1],
                    "bbox": best["bbox"],
                    "match_text": best["text"],
                    "confidence": best["confidence"],
                    "elapsed_s": round(
                        timeout_s - (deadline - time.perf_counter()),
                        2,
                    ),
                    "attempts": attempts,
                }, ensure_ascii=False))
            await asyncio.sleep(poll)
        return _fail(call, t0, json.dumps({
            "found": False,
            "wanted": text,
            "timed_out_after_s": timeout_s,
            "attempts": attempts,
            "sample_blocks_last_poll": [
                {"text": b["text"], "confidence": b["confidence"]}
                for b in last_blocks[:10]
            ],
        }, ensure_ascii=False))

    async def _screen_region_capture(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        try:
            region = args["region"]
            x, y, w, h = (int(v) for v in region)
        except (KeyError, TypeError, ValueError):
            return _fail(
                call, t0,
                "region=[x, y, w, h] (4 ints) required",
            )
        if w <= 0 or h <= 0:
            return _fail(call, t0, "region width/height must be > 0")
        # B-Vision: same migration as _screen_capture — base64 in tool
        # text is the wrong channel; hop_loop attaches the file as a
        # real vision content block instead.
        include_b64 = bool(args.get("include_base64", False))
        quality = _clamp(int(args.get("quality", 85)), 1, 100)

        try:
            import mss
        except ImportError:
            return _fail(
                call, t0,
                "screen_region_capture needs ``mss``: pip install mss",
            )
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            return _fail(
                call, t0,
                "screen_region_capture needs ``Pillow``: pip install Pillow",
            )

        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        out = self._screenshot_dir / f"{int(time.time())}_{call.id[:8]}.jpg"

        def _capture() -> tuple[int, int, int]:
            from PIL import Image as _Image
            with mss.mss() as sct:
                shot = sct.grab({
                    "left": x, "top": y, "width": w, "height": h,
                })
                img = _Image.frombytes(
                    "RGB", shot.size,
                    shot.bgra, "raw", "BGRX",
                )
                img.save(out, format="JPEG", quality=int(quality))
            return (shot.size[0], shot.size[1], out.stat().st_size)

        try:
            (rw, rh, fsize) = await asyncio.to_thread(_capture)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"region capture failed: {type(exc).__name__}: {exc}",
            )

        result: dict[str, Any] = {
            "path": str(out),
            "region": [x, y, w, h],
            "size": [rw, rh],
            "bytes": fsize,
            "vision_attached": True,
        }
        if include_b64 and fsize <= self._base64_size_cap:
            try:
                import base64 as _b64
                result["base64_jpg"] = _b64.b64encode(
                    out.read_bytes(),
                ).decode("ascii")
            except OSError:
                pass
        return ToolResult(
            call_id=call.id, ok=True,
            content=json.dumps(result, ensure_ascii=False),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata={"attach_image": str(out)},
        )

    # ── 2026-05-12 r2: image template matching ────────────────────

    async def _find_image_on_screen(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        template_path = args.get("template_path")
        if not isinstance(template_path, str) or not template_path:
            return _fail(call, t0, "template_path (string) required")
        tpath = Path(template_path).expanduser()
        if not tpath.is_file():
            return _fail(call, t0, f"template not found: {tpath}")
        confidence = _clamp(float(args.get("confidence", 0.8)), 0.1, 1.0)
        region = args.get("region")

        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:
            return _fail(
                call, t0,
                f"find_image_on_screen needs ``opencv-python``: {exc}",
            )

        def _do_find() -> dict[str, Any]:
            template = cv2.imread(str(tpath), cv2.IMREAD_COLOR)
            if template is None:
                raise RuntimeError(f"cv2 couldn't read template {tpath}")
            th, tw = template.shape[:2]
            # Grab full screen / region as BGR ndarray
            screen, (ox, oy) = _grab_for_ocr(region)
            res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
            if max_val < confidence:
                return {
                    "found": False,
                    "best_confidence": round(float(max_val), 4),
                    "threshold": confidence,
                }
            x0, y0 = int(max_loc[0]) + ox, int(max_loc[1]) + oy
            return {
                "found": True,
                "x": x0 + tw // 2,
                "y": y0 + th // 2,
                "bbox": [x0, y0, tw, th],
                "confidence": round(float(max_val), 4),
            }

        try:
            payload = await asyncio.to_thread(_do_find)
        except RuntimeError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"find_image_on_screen failed: {type(exc).__name__}: {exc}",
            )

        if not payload["found"]:
            return _fail(call, t0, json.dumps(payload))
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

    async def _click_on_image(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        find_args = {
            k: v for k, v in args.items()
            if k in ("template_path", "confidence", "region")
        }
        find_call = ToolCall(
            id=call.id + "-find",
            name="find_image_on_screen",
            args=find_args,
            provenance=call.provenance,
            session_id=call.session_id,
        )
        fr = await self._find_image_on_screen(find_call, t0, find_args)
        if not fr.ok:
            return _fail(call, t0, fr.error)
        fp = json.loads(fr.content)
        button = str(args.get("button", "left"))
        if button not in _VALID_BUTTONS:
            return _fail(call, t0, f"button must be one of {sorted(_VALID_BUTTONS)}")
        count = _clamp(int(args.get("count", 1)), 1, 3)
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        x, y = fp["x"], fp["y"]
        try:
            await asyncio.to_thread(
                pg.click, x=x, y=y, button=button, clicks=count,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"click at ({x},{y}) failed: {type(exc).__name__}: {exc}",
            )
        return _ok(call, t0, json.dumps({
            "clicked": True, "x": x, "y": y,
            "button": button, "count": count,
            "template": args.get("template_path"),
            "confidence": fp["confidence"],
            "bbox": fp["bbox"],
        }, ensure_ascii=False))

    # ── 2026-05-12 r2: scroll_to_text ──────────────────────────────

    async def _scroll_to_text(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(call, t0, "text (non-empty string) required")
        direction = str(args.get("direction", "down")).lower()
        if direction not in ("up", "down"):
            return _fail(call, t0, "direction must be 'up' or 'down'")
        max_scrolls = _clamp(int(args.get("max_scrolls", 10)), 1, 30)
        scroll_amount = int(args.get("scroll_amount", 3))
        if scroll_amount <= 0:
            return _fail(call, t0, "scroll_amount must be > 0")
        clicks = -scroll_amount if direction == "down" else scroll_amount
        region = args.get("region")
        exact = bool(args.get("exact", False))
        x = args.get("x")
        y = args.get("y")

        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))

        # First check — might already be visible without any scroll.
        for attempt in range(max_scrolls + 1):
            try:
                blocks = await asyncio.to_thread(
                    _run_ocr_full_pipeline, region, 0.5,
                )
            except _NoOCREngineError as exc:
                return _fail(call, t0, str(exc))
            except Exception:  # noqa: BLE001
                blocks = []
            matches = _match_text_in_blocks(blocks, text, exact=exact)
            if matches:
                best = matches[0]
                return _ok(call, t0, json.dumps({
                    "found": True,
                    "x": best["center"][0],
                    "y": best["center"][1],
                    "bbox": best["bbox"],
                    "match_text": best["text"],
                    "confidence": best["confidence"],
                    "scrolls_tried": attempt,
                }, ensure_ascii=False))
            if attempt >= max_scrolls:
                break
            # Scroll and retry
            try:
                if x is not None and y is not None:
                    await asyncio.to_thread(
                        pg.scroll, clicks, x=int(x), y=int(y),
                    )
                else:
                    await asyncio.to_thread(pg.scroll, clicks)
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"scroll failed: {exc}")
            # Small pause for repaint
            await asyncio.sleep(0.25)

        return _fail(call, t0, json.dumps({
            "found": False,
            "wanted": text,
            "scrolls_tried": max_scrolls,
            "direction": direction,
        }, ensure_ascii=False))

    # ── 2026-05-12 r2: Windows UIAutomation ────────────────────────

    async def _ui_inspect(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        try:
            import uiautomation as uia  # type: ignore
        except ImportError:
            return _fail(call, t0, (
                "ui_inspect needs ``uiautomation``: "
                "pip install uiautomation (Windows only)"
            ))
        control_type = (args.get("control_type") or "").strip()
        name_contains = (args.get("name_contains") or "").strip().lower()
        window_title = (args.get("window_title") or "").strip()
        max_depth = _clamp(int(args.get("max_depth", 6)), 1, 12)

        def _do_inspect() -> dict[str, Any]:
            # COM threading: uiautomation needs CoInitialize() per thread.
            # asyncio.to_thread spawns a worker without it; the library
            # ships ``UIAutomationInitializerInThread`` as the context-
            # manager that handles both Co{,Un}Initialize. Without it
            # every UIA call raises "CoInitialize hasn't been called".
            with uia.UIAutomationInitializerInThread():
                if window_title:
                    root = uia.WindowControl(
                        searchDepth=2, SubName=window_title,
                    )
                    if not root.Exists(maxSearchSeconds=1):
                        raise LookupError(
                            f"no window title containing {window_title!r}",
                        )
                else:
                    root = uia.GetForegroundControl()

                target_title = ""
                try:
                    cur = root
                    for _ in range(5):
                        if cur is None:
                            break
                        target_title = getattr(cur, "Name", "") or target_title
                        if cur.ControlTypeName == "WindowControl":
                            break
                        cur = cur.GetParentControl() if hasattr(cur, "GetParentControl") else None
                except Exception:  # noqa: BLE001
                    pass

                elements: list[dict[str, Any]] = []

                def _walk(ctrl, depth: int) -> None:
                    if depth > max_depth or len(elements) >= 100:
                        return
                    try:
                        name = getattr(ctrl, "Name", "") or ""
                        ctype = getattr(ctrl, "ControlTypeName", "") or ""
                        auto_id = getattr(ctrl, "AutomationId", "") or ""
                        bbox = getattr(ctrl, "BoundingRectangle", None)
                    except Exception:  # noqa: BLE001
                        return
                    keep = True
                    if control_type and ctype.lower() != f"{control_type.lower()}control":
                        if not ctype.lower().startswith(control_type.lower()):
                            keep = False
                    if keep and name_contains and name_contains not in name.lower():
                        keep = False
                    if keep and (name or auto_id):
                        bbox_list = [0, 0, 0, 0]
                        if bbox is not None:
                            try:
                                bbox_list = [
                                    int(bbox.left),
                                    int(bbox.top),
                                    int(bbox.right - bbox.left),
                                    int(bbox.bottom - bbox.top),
                                ]
                            except Exception:  # noqa: BLE001
                                pass
                        elements.append({
                            "name": name[:120],
                            "control_type": ctype,
                            "automation_id": auto_id[:80],
                            "bbox": bbox_list,
                            "depth": depth,
                        })
                    try:
                        for child in ctrl.GetChildren():
                            _walk(child, depth + 1)
                            if len(elements) >= 100:
                                return
                    except Exception:  # noqa: BLE001
                        return

                _walk(root, 0)
                return {
                    "window_title": target_title[:160],
                    "count": len(elements),
                    "elements": elements,
                }

        try:
            payload = await asyncio.to_thread(_do_inspect)
        except LookupError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"ui_inspect failed: {type(exc).__name__}: {exc}",
            )
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

    async def _ui_click(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        try:
            import uiautomation as uia  # type: ignore
        except ImportError:
            return _fail(call, t0, (
                "ui_click needs ``uiautomation``: "
                "pip install uiautomation (Windows only)"
            ))
        name_contains = (args.get("name_contains") or "").strip()
        automation_id = (args.get("automation_id") or "").strip()
        control_type = (args.get("control_type") or "").strip()
        window_title = (args.get("window_title") or "").strip()
        double_click = bool(args.get("double_click", False))
        if not (name_contains or automation_id):
            return _fail(
                call, t0,
                "need name_contains or automation_id",
            )

        def _do_click() -> dict[str, Any]:
            # COM threading guard — see _ui_inspect for the same pattern.
            with uia.UIAutomationInitializerInThread():
                if window_title:
                    root = uia.WindowControl(searchDepth=2, SubName=window_title)
                    if not root.Exists(maxSearchSeconds=2):
                        raise LookupError(
                            f"window {window_title!r} not found",
                        )
                else:
                    root = uia.GetForegroundControl()

                # Walk tree looking for matching element
                matched = []

                def _walk(ctrl, depth: int) -> None:
                    if depth > 10 or len(matched) > 5:
                        return
                    try:
                        name = getattr(ctrl, "Name", "") or ""
                        ctype = getattr(ctrl, "ControlTypeName", "") or ""
                        auto_id = getattr(ctrl, "AutomationId", "") or ""
                    except Exception:  # noqa: BLE001
                        return
                    hit = False
                    if name_contains and name_contains.lower() in name.lower():
                        hit = True
                    if automation_id and automation_id == auto_id:
                        hit = True
                    if hit and (
                        not control_type
                        or control_type.lower() in ctype.lower()
                    ):
                        matched.append((ctrl, name, ctype, auto_id))
                    try:
                        for child in ctrl.GetChildren():
                            _walk(child, depth + 1)
                    except Exception:  # noqa: BLE001
                        return

                _walk(root, 0)
                if not matched:
                    raise LookupError(
                        f"no UI element matching name_contains="
                        f"{name_contains!r} automation_id="
                        f"{automation_id!r} control_type={control_type!r}",
                    )
                ctrl, name, ctype, auto_id = matched[0]
                try:
                    bbox = ctrl.BoundingRectangle
                    bbox_list = [
                        int(bbox.left), int(bbox.top),
                        int(bbox.right - bbox.left),
                        int(bbox.bottom - bbox.top),
                    ]
                except Exception:  # noqa: BLE001
                    bbox_list = [0, 0, 0, 0]
                # Prefer InvokePattern (no mouse movement, more
                # reliable) but fall back to physical click when the
                # control doesn't expose it.
                invoked = False
                try:
                    ctrl.SetFocus()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if hasattr(ctrl, "GetInvokePattern"):
                        pattern = ctrl.GetInvokePattern()
                        if pattern is not None:
                            pattern.Invoke()
                            invoked = True
                except Exception:  # noqa: BLE001
                    pass
                if not invoked:
                    try:
                        if double_click:
                            ctrl.DoubleClick()
                        else:
                            ctrl.Click()
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"both InvokePattern and physical click failed: "
                            f"{exc}",
                        )
                return {
                    "clicked": True,
                    "name": name[:120],
                    "control_type": ctype,
                    "automation_id": auto_id[:80],
                    "bbox": bbox_list,
                    "via": "invoke_pattern" if invoked else "physical_click",
                }

        try:
            payload = await asyncio.to_thread(_do_click)
        except (LookupError, RuntimeError) as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"ui_click failed: {type(exc).__name__}: {exc}",
            )
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))


# ── Helpers ───────────────────────────────────────────────────────────


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _pg_install_hint(exc: ImportError) -> str:
    """Build a platform-aware pyautogui install hint."""
    sysname = platform.system()
    extras = ""
    if sysname == "Darwin":
        extras = (
            " — on macOS also grant Accessibility + Screen Recording "
            "permission to your terminal in System Settings → Privacy."
        )
    elif sysname == "Linux":
        extras = (
            " — on Linux also: ``sudo apt install python3-tk python3-dev`` "
            "and an X11 session (Wayland needs ``xdotool``)."
        )
    return (
        f"computer_use tool needs ``pyautogui``: pip install pyautogui"
        f"{extras}\n  (underlying error: {exc})"
    )


def _ok(call: ToolCall, t0: float, content: Any) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=True, content=content,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ── 2026-05-12 OCR backend ────────────────────────────────────────────


class _NoOCREngineError(RuntimeError):
    """Raised when none of the supported OCR engines can be imported."""


def _grab_for_ocr(region: list | tuple | None) -> Any:
    """Capture full-screen or region as numpy ndarray (H, W, 3) BGR.

    RapidOCR / PaddleOCR both accept numpy arrays directly; pytesseract
    expects a PIL Image and we adapt below.
    """
    import mss
    import numpy as np
    with mss.mss() as sct:
        if region:
            x, y, w, h = (int(v) for v in region)
            mon = {"left": x, "top": y, "width": w, "height": h}
        else:
            mon = sct.monitors[1]  # primary
        shot = sct.grab(mon)
        # mss returns BGRA; OCR engines want BGR / RGB.
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
            shot.size[1], shot.size[0], 4,
        )
        # BGRA → BGR (drop alpha). RapidOCR + PaddleOCR are OK with BGR.
        return arr[:, :, :3], (mon.get("left", 0), mon.get("top", 0))


def _run_ocr_full_pipeline(
    region: list | tuple | None, min_confidence: float,
) -> list[dict]:
    """Run OCR + return blocks with absolute screen coordinates.

    Tries engines in order: rapidocr-onnxruntime → paddleocr →
    pytesseract. Each block: {text, bbox: [x, y, w, h], center: [cx,
    cy], confidence}. Coordinates are ABSOLUTE screen pixels (i.e.
    region offset is already added).
    """
    img, (ox, oy) = _grab_for_ocr(region)
    # Try rapidocr first — best Chinese support per MB.
    blocks = _try_rapidocr(img, min_confidence)
    if blocks is not None:
        return _offset_blocks(blocks, ox, oy)
    blocks = _try_paddleocr(img, min_confidence)
    if blocks is not None:
        return _offset_blocks(blocks, ox, oy)
    blocks = _try_pytesseract(img, min_confidence)
    if blocks is not None:
        return _offset_blocks(blocks, ox, oy)
    raise _NoOCREngineError(
        "No OCR engine installed. Pick one:\n"
        "  pip install rapidocr-onnxruntime   # 50 MB, best Chinese support\n"
        "  pip install paddleocr              # 300 MB, most accurate\n"
        "  pip install pytesseract             # needs Tesseract binary + chi_sim data\n"
        "(rapidocr is recommended — bundled into xmclaw[computer-use].)"
    )


def _try_rapidocr(img: Any, min_confidence: float) -> list[dict] | None:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    try:
        engine = RapidOCR()
        result, _elapse = engine(img)
        if not result:
            return []
        blocks: list[dict] = []
        for row in result:
            # rapidocr row: [bbox4points, text, score]
            if not row or len(row) < 3:
                continue
            pts, text, score = row[0], row[1], row[2]
            if score is None or score < min_confidence:
                continue
            # bbox4points = [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]; compute axis-aligned
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x0, y0 = int(min(xs)), int(min(ys))
            x1, y1 = int(max(xs)), int(max(ys))
            blocks.append({
                "text": str(text),
                "bbox": [x0, y0, x1 - x0, y1 - y0],
                "center": [(x0 + x1) // 2, (y0 + y1) // 2],
                "confidence": round(float(score), 4),
                "engine": "rapidocr",
            })
        return blocks
    except Exception:  # noqa: BLE001 — fall through to next engine
        return None


def _try_paddleocr(img: Any, min_confidence: float) -> list[dict] | None:
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        return None
    try:
        # use_angle_cls=False for speed; lang="ch" handles both EN + CN
        engine = PaddleOCR(use_angle_cls=False, lang="ch")
        result = engine.ocr(img, cls=False)
        if not result:
            return []
        blocks: list[dict] = []
        for page in result:
            if not page:
                continue
            for row in page:
                if not row or len(row) < 2:
                    continue
                pts, (text, score) = row[0], row[1]
                if score < min_confidence:
                    continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                x0, y0 = int(min(xs)), int(min(ys))
                x1, y1 = int(max(xs)), int(max(ys))
                blocks.append({
                    "text": str(text),
                    "bbox": [x0, y0, x1 - x0, y1 - y0],
                    "center": [(x0 + x1) // 2, (y0 + y1) // 2],
                    "confidence": round(float(score), 4),
                    "engine": "paddleocr",
                })
        return blocks
    except Exception:  # noqa: BLE001
        return None


def _try_pytesseract(img: Any, min_confidence: float) -> list[dict] | None:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        # pytesseract wants PIL; convert from numpy BGR
        pil_img = Image.fromarray(img[:, :, ::-1])  # BGR → RGB
        data = pytesseract.image_to_data(
            pil_img,
            lang="chi_sim+eng",  # Chinese + English; OK to fall back if chi_sim absent
            output_type=pytesseract.Output.DICT,
        )
        blocks: list[dict] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            conf_raw = data["conf"][i]
            try:
                conf = float(conf_raw) / 100.0  # tesseract returns 0-100
            except (TypeError, ValueError):
                conf = 0.0
            if conf < min_confidence:
                continue
            x = int(data["left"][i])
            y = int(data["top"][i])
            w = int(data["width"][i])
            h = int(data["height"][i])
            blocks.append({
                "text": text,
                "bbox": [x, y, w, h],
                "center": [x + w // 2, y + h // 2],
                "confidence": round(conf, 4),
                "engine": "pytesseract",
            })
        return blocks
    except Exception:  # noqa: BLE001
        return None


def _offset_blocks(blocks: list[dict], ox: int, oy: int) -> list[dict]:
    """Shift block coordinates by region offset so the LLM gets
    absolute-screen coordinates regardless of whether OCR was full or
    region-cropped."""
    if ox == 0 and oy == 0:
        return blocks
    for b in blocks:
        b["bbox"][0] += ox
        b["bbox"][1] += oy
        b["center"][0] += ox
        b["center"][1] += oy
    return blocks


def _match_text_in_blocks(
    blocks: list[dict], wanted: str, *, exact: bool = False,
) -> list[dict]:
    """Find OCR blocks matching ``wanted``. Returns matches sorted
    by confidence desc; empty list when nothing matches.

    - ``exact=False`` (default): case-insensitive substring match.
      Most useful for GUI clicking — OCR may read "魔丸群 (12)" when
      you wanted "魔丸群".
    - ``exact=True``: trimmed-equal match. For when you really mean it.
    """
    wanted_norm = wanted.strip().casefold()
    if not wanted_norm:
        return []
    matches: list[dict] = []
    for b in blocks:
        text_norm = b["text"].strip().casefold()
        if exact:
            if text_norm == wanted_norm:
                matches.append(b)
        else:
            if wanted_norm in text_norm:
                matches.append(b)
    matches.sort(key=lambda m: m["confidence"], reverse=True)
    return matches


__all__ = ["ComputerUseTools"]
