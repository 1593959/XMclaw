# 2026-06-18 refactor: 23 tools → 1 unified computer_use tool + action param.
# Adds: capture_after, backend sticky window state, SOM element indexing,
# dangerous-action hard blocks, non-vision OCR fallback.
# Old tool names remain as a backward-compatible deprecation layer.
# Stage 1 fix: multi-scale template + click retry + fuzzy OCR + pixel verify
"""ComputerUseTools — give the agent a mouse, a keyboard, and eyes.

2026-06-18. REFACTOR: list_tools() now returns a single ``computer_use``
ToolSpec. The previous 23 individual tools (screen_capture, mouse_click,
etc.) are mapped internally to ``action`` sub-commands with deprecation
warnings. New features: ``capture_after`` auto-screenshot on mutating
actions, backend sticky window state, SOM element indexing, and
dangerous-action hard blocks.

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
import difflib
import hashlib
import json
import os
import platform
import time
import warnings
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
        "Take a full-screen screenshot. Returns {path, size: [w, h], "
        "monitor_index, vision_attached}. Default captures the primary "
        "monitor (index 1 in mss). Pass ``monitor`` to pick a specific "
        "monitor (0 = virtual screen union of all monitors).\n\n"
        "The image is automatically attached to the NEXT turn as a "
        "real vision content block — the model literally SEES it. You "
        "do NOT need to opt in; just call this and look at the next "
        "turn. Use the returned ``path`` if you need to pipe it into "
        "another tool (OCR, region crop, etc.).\n\n"
        "Coordinate mapping: for the primary monitor the result "
        "includes ``pyautogui_size`` and ``click_scale`` [sx, sy]. "
        "When click_scale is [1, 1] (the normal case — the provider "
        "makes the process DPI-aware), coordinates you read off the "
        "screenshot can be passed to mouse_* tools as-is. Otherwise "
        "multiply screenshot coords by click_scale first."
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
                "description": (
                    "Include raw base64 bytes in the result. Default "
                    "FALSE. Almost always leave this off — base64 "
                    "explodes the prompt and the LLM cannot read it "
                    "from a tool result anyway. Vision is delivered "
                    "via the multimodal pipeline regardless of this "
                    "flag. Set true ONLY if downstream non-LLM code "
                    "consumes the bytes."
                ),
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
        "guarantees the click lands on the intended pixel.\n\n"
        "**Grounding loop (follow this discipline):** ① screen_capture "
        "and READ the screenshot to locate the target (mind "
        "``click_scale`` if it isn't [1,1]); ② act; ③ VERIFY — pass "
        "``verify_text`` (text that should appear after a successful "
        "click, e.g. a dialog title) or re-capture and look. Never "
        "assume a click worked; ``verified: false`` in the result "
        "means re-look before retrying."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "count": {"type": "integer", "description": "1-3"},
            "verify_text": {
                "type": "string",
                "description": (
                    "Optional. After the click, poll OCR until this "
                    "text appears on screen (success signal, e.g. a "
                    "dialog title that should open). Result gains "
                    "``verified: true/false`` + diagnostics."
                ),
            },
            "verify_timeout_s": {
                "type": "number",
                "description": "Verify polling window, 0.5-30s, default 5.",
            },
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
        "isn't found — the LLM can adjust its query and retry.\n\n"
        "Pass ``verify_text`` to confirm the click had its intended "
        "effect (poll OCR for a success signal after clicking, e.g. "
        "the window title that should open). ``verified: false`` in "
        "the result = the click landed but the expected state never "
        "showed — re-capture and re-plan instead of assuming success."
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
            "verify_text": {
                "type": "string",
                "description": (
                    "Optional post-click success signal: poll OCR "
                    "until this text appears."
                ),
            },
            "verify_timeout_s": {
                "type": "number",
                "description": "Verify polling window, 0.5-30s, default 5.",
            },
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
        "{path, region, size, bytes, vision_attached}. Useful when "
        "you've already OCR'd and want to send the LLM only the "
        "relevant pane.\n\n"
        "The cropped image is automatically attached to the NEXT "
        "turn as a vision content block — you do NOT need to opt in."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "region": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "[x, y, w, h]",
            },
            "include_base64": {
                "type": "boolean",
                "description": (
                    "Default FALSE. Almost always leave off — see "
                    "screen_capture for why."
                ),
            },
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


_GUI_SEND_CHAT_SPEC = ToolSpec(
    name="gui_send_chat",
    description=(
        "ATOMIC navigate-and-send for chat apps (WeChat / QQ / 飞书 / "
        "Discord / Slack desktop / Telegram / etc.). One tool call "
        "does the whole sequence: focus window → optionally NAVIGATE "
        "to the target chat by name → OCR-verify chat header → click "
        "input box → type text via clipboard → press Enter → "
        "confirmation screenshot. **The recommended call for a fresh "
        "send is:** ``gui_send_chat(text=\"...\", window_title=\"WeChat\", "
        "nav_chat_name=\"<group>\", verify_chat_title=\"<group>\")`` — "
        "this is the SINGLE atomic call that replaces the 4-step "
        "manual chain (window_focus + click_on_text + mouse_click + "
        "keyboard_type+enter) and avoids every known failure mode.\n\n"
        "**SAFETY RAIL — always pass ``verify_chat_title``** when you "
        "know the target chat name. The tool OCRs a narrow strip at "
        "the top of the focused window (the chat header, ~80 px tall) "
        "and ABORTS the send if the title substring is not present. "
        "This is the one defense that stops the WeChat-specific "
        "failure where the chat list scrolls under the agent and a "
        "stale click coordinate lands on the wrong conversation.\n\n"
        "Input-box location: if ``input_bbox`` is given, click its "
        "center (preferred — read it visually from a screenshot first). "
        "Otherwise use a HEURISTIC: 70 px above the bottom edge of "
        "the focused window, horizontally centered. The heuristic is "
        "fragile when the window is non-maximized or split-pane.\n\n"
        "Typing uses the system clipboard + Ctrl+V — pyautogui's "
        "per-key write is unreliable for Chinese / IME input."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Message body to send. Required.",
            },
            "window_title": {
                "type": "string",
                "description": (
                    "Substring of the target window title to focus "
                    "first. Optional — if omitted we use the current "
                    "foreground window."
                ),
            },
            "nav_chat_name": {
                "type": "string",
                "description": (
                    "If set, ATOMIC navigate-then-send: OCR the chat "
                    "list (left pane of the focused window), find this "
                    "substring, click it, wait for the conversation to "
                    "load, then type + send. Eliminates the stale-"
                    "coords / wrong-chat bug entirely — pass the group "
                    "or contact name (e.g. \"魔丸\") and the tool "
                    "handles navigation + send in one hop. Typically "
                    "this is the SAME value you'd pass to "
                    "verify_chat_title (which is checked after the "
                    "navigation click). When in doubt, set both to "
                    "the chat name."
                ),
            },
            "verify_chat_title": {
                "type": "string",
                "description": (
                    "If set, OCR a narrow chat-header strip on the "
                    "focused window and ABORT the send if this "
                    "substring is not visible. Pass this whenever you "
                    "know the target chat name (e.g. \"魔丸\"). It is "
                    "the cheapest single defense against the wrong-"
                    "chat-send bug. Use TOGETHER with nav_chat_name "
                    "for full atomic safety."
                ),
            },
            "input_bbox": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "[x, y, w, h] of the input box in screen pixels. "
                    "Overrides the bottom-anchored heuristic when set. "
                    "Strongly preferred — read this off a screenshot."
                ),
            },
            "press_after": {
                "type": "string",
                "description": (
                    "Key pressed after typing. Default 'enter' "
                    "(sends in most chat apps). Set to '' to skip "
                    "and leave the text in the input box for manual "
                    "review."
                ),
            },
            "confirm_screenshot": {
                "type": "boolean",
                "description": (
                    "If true (default), take a screenshot AFTER "
                    "sending and attach it (via "
                    "metadata.attach_image) so you can confirm the "
                    "message landed. Set false only if you really "
                    "don't need verification."
                ),
            },
        },
        "required": ["text"],
    },
)

_LEGACY_TOOL_MAP: dict[str, tuple[str, dict]] = {
    "screen_capture":       ("capture", {"mode": "vision"}),
    "screen_size":          ("screen_size", {}),
    "cursor_position":      ("cursor_position", {}),
    "mouse_move":           ("move", {}),
    "mouse_click":          ("click", {}),
    "mouse_drag":           ("drag", {}),
    "mouse_scroll":         ("scroll", {}),
    "keyboard_type":        ("type", {}),
    "keyboard_press":       ("key", {}),
    "window_list":          ("list_windows", {}),
    "window_focus":         ("focus_window", {}),
    "screen_ocr":           ("ocr", {}),
    "find_on_screen":       ("find_text", {}),
    "click_on_text":        ("click_text", {}),
    "wait_for_text":        ("wait_for_text", {}),
    "screen_region_capture": ("region_capture", {}),
    "find_image_on_screen": ("find_image", {}),
    "click_on_image":       ("click_image", {}),
    "scroll_to_text":       ("scroll_to_text", {}),
    "ui_inspect":           ("ui_inspect", {}),
    "ui_click":             ("ui_click", {}),
    "gui_send_chat":        ("gui_send_chat", {}),
}

_READ_ONLY_ACTIONS = {
    "observe", "capture", "screen_size", "cursor_position", "list_windows",
    "window_list", "ocr", "find_text", "find_on_screen", "region_capture",
    "screen_region_capture", "find_image", "find_image_on_screen",
    "ui_inspect",
}

_MUTATING_ACTIONS = {
    "move", "click", "double_click", "right_click", "drag", "scroll",
    "type", "key", "click_text", "click_image", "ui_click", "gui_send_chat",
    "focus_window", "wait_for_text",
}

_COMPUTER_USE_SPEC = ToolSpec(
    name="computer_use",
    description=("""控制本地桌面：截图、点击、输入、滚动、应用管理。

核心参数：
- action: observe | capture | click | double_click | right_click | scroll | type | key | wait | list_windows | focus_window | screen_size | cursor_position | move | drag | ocr | find_text | click_text | wait_for_text | region_capture | find_image | click_image | scroll_to_text | ui_inspect | ui_click | gui_send_chat
- element: 元素索引（SOM 模式截图后返回的编号）
- coordinate: [x, y]（坐标模式 fallback）
- capture_after: 动作后自动截图并返回（默认 true，对 mutating 动作）
- mode: "som" | "vision"（截图模式）
- text: type 动作的文本
- keys: key 动作的按键（如 "enter", "ctrl+v"）
- monitor: 截图显示器索引（默认 1 = primary）
- max_elements: SOM 模式下最大元素数（默认 50，上限 200）
- vision: 是否返回图片（默认 true；设为 false 时非 vision 模型回退到 OCR 文本）

使用流程：
1. computer_use(action="capture", mode="som") → 返回截图 + 元素列表
2. computer_use(action="click", element=3, capture_after=True) → 点击元素3，返回新截图
3. computer_use(action="type", text="hello") → 输入文本
4. computer_use(action="key", keys="enter") → 按键
"""),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Sub-action to perform. One of: capture, click, double_click, "
                    "observe, capture, click, double_click, right_click, scroll, type, key, wait, list_windows, focus_window, "
                    "screen_size, cursor_position, move, drag, ocr, find_text, click_text, "
                    "wait_for_text, region_capture, find_image, click_image, scroll_to_text, "
                    "ui_inspect, ui_click, gui_send_chat"
                ),
            },
            "element": {
                "type": "integer",
                "description": "SOM element index (from a prior capture with mode=som).",
            },
            "coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "[x, y] screen coordinates. Used when element is not provided.",
            },
            "capture_after": {
                "type": "boolean",
                "description": (
                    "If true, automatically capture a screenshot AFTER the action and "
                    "attach it (metadata.attach_image). Defaults to true for mutating "
                    "actions (click, type, key, scroll, move, drag, etc.)."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["som", "vision"],
                "description": "Screenshot mode. 'som' overlays numbered element circles.",
            },
            "text": {
                "type": "string",
                "description": "Text to type (for action=type).",
            },
            "keys": {
                "type": "string",
                "description": "Key or chord to press (for action=key), e.g. 'enter', 'ctrl+v'.",
            },
            "monitor": {
                "type": "integer",
                "description": "mss monitor index (default 1 = primary).",
            },
            "max_elements": {
                "type": "integer",
                "description": "Max elements in SOM mode (default 50, hard cap 200).",
            },
            "vision": {
                "type": "boolean",
                "description": (
                    "If false and action=capture, return OCR text description instead of "
                    "an image (non-vision model fallback)."
                ),
            },
            "title_contains": {
                "type": "string",
                "description": "Substring for window matching.",
            },
            "window_title": {
                "type": "string",
                "description": "Window title for focus / inspect.",
            },
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "duration": {"type": "number", "description": "Mouse move duration (0-30s)."},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "count": {"type": "integer", "description": "Click count (1-3)."},
            "amount": {"type": "integer", "description": "Scroll amount (+/- clicks)."},
            "interval": {"type": "number", "description": "Typing interval between keys."},
            "include_base64": {"type": "boolean"},
            "min_confidence": {"type": "number"},
            "exact": {"type": "boolean"},
            "region": {"type": "array", "items": {"type": "integer"}},
            "verify_pixel": {"type": "boolean"},
            "verify_text": {"type": "string"},
            "nav_chat_name": {"type": "string"},
            "verify_chat_title": {"type": "string"},
            "input_bbox": {"type": "array", "items": {"type": "integer"}},
            "press_after": {"type": "string"},
            "confirm_screenshot": {"type": "boolean"},
            "control_type": {"type": "string"},
            "name_contains": {"type": "string"},
            "max_depth": {"type": "integer"},
        },
        "required": ["action"],
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
        # B-TOPMOST-CLEANUP: gui_send_chat pins WeChat HWND_TOPMOST so
        # Claude Code / browser overlays can't obscure it during the
        # OCR-+-click sequence. We track the pinned window here and
        # release at the START of the NEXT invoke (cheap, simple, and
        # guaranteed even if gui_send_chat errored out before reaching
        # its own release).
        self._pending_topmost_release: Any | None = None
        # ── 2026-06-18 refactor: backend sticky state ───────────────────
        self._active_hwnd: int | None = None
        self._active_pid: int | None = None
        self._last_window_title: str | None = None
        # SOM sticky state for element indexing
        self._last_som_elements: list[dict[str, Any]] = []
        self._last_som_overlay_path: str | None = None
        # Unified computer-use runtime state: compact action log,
        # last screen hash, and repeated no-change counter so the
        # planner can switch routes instead of retrying the same click.
        self._action_log: list[dict[str, Any]] = []
        self._last_observation: dict[str, Any] | None = None
        self._last_screen_hash: str | None = None
        self._no_change_streak: int = 0

    def list_tools(self) -> list[ToolSpec]:
        return [_COMPUTER_USE_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        name = call.name
        args = call.args or {}
        # B-TOPMOST-CLEANUP: release any window left HWND_TOPMOST by
        # the previous gui_send_chat call. The pin is intentionally
        # left in place until the NEXT tool invocation so the agent /
        # user can see the message land + read confirmation; by the
        # time another tool fires the user has had visual feedback.
        prev = self._pending_topmost_release
        if prev is not None:
            self._pending_topmost_release = None
            try:
                await asyncio.to_thread(_release_topmost, prev)
            except Exception:  # noqa: BLE001 — never block the new tool
                pass
        # 2026-06-18 refactor: backward-compat deprecation layer
        if name in _LEGACY_TOOL_MAP:
            warnings.warn(
                f"Tool {name!r} is deprecated; use "
                f"computer_use(action={_LEGACY_TOOL_MAP[name][0]!r})",
                DeprecationWarning,
                stacklevel=2,
            )
            action, defaults = _LEGACY_TOOL_MAP[name]
            merged = {**defaults, **args, "action": action}
            return await self._computer_use(call, t0, merged)
        if name == "computer_use":
            return await self._computer_use(call, t0, args)
        # Fallback: old direct dispatch (kept for safety/tests)
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
            if name == "gui_send_chat":         return await self._gui_send_chat(call, t0, args)
        except Exception as exc:  # noqa: BLE001 — surface as ok=False
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")
        return _fail(call, t0, f"unknown tool: {name!r}")

    # ── 2026-06-18 unified action dispatcher ────────────────────────────

    def _danger_check(self, action: str, args: dict) -> str | None:
        """Hard-block dangerous shortcuts / shell commands."""
        if action in ("key", "keyboard_press"):
            keys = str(args.get("keys", "")).strip().lower().replace(" ", "+")
            # Exact blacklist
            for blocked in ("alt+f4", "win+l", "ctrl+alt+del", "ctrl+alt+delete"):
                if blocked in keys:
                    return f"blacklisted keyboard shortcut: {keys!r}"
            # Part-based checks
            parts = [p.strip() for p in keys.split("+") if p.strip()]
            if "alt" in parts and "f4" in parts:
                return "blacklisted keyboard shortcut: alt+f4"
            if "win" in parts and "l" in parts:
                return "blacklisted keyboard shortcut: win+l"
            if "ctrl" in parts and "alt" in parts and ("del" in parts or "delete" in parts):
                return "blacklisted keyboard shortcut: ctrl+alt+del"
        if action in ("type", "keyboard_type"):
            text = str(args.get("text", "")).lower()
            # Shell pipe patterns
            for a, b, c in (("curl", "|", "bash"), ("curl", "|", "sh"),
                            ("wget", "|", "bash"), ("wget", "|", "sh")):
                if a in text and b in text and c in text:
                    return f"dangerous shell pipe pattern: {a} ... {b} ... {c}"
            if "rm -rf /" in text or "rm -rf /" in text.replace(" ", ""):
                return "dangerous command: rm -rf /"
            if "dd if=" in text and "of=" in text:
                return "dangerous disk command: dd"
        return None

    async def _quick_capture(self) -> str:
        """Lightweight screenshot for capture_after / sticky state."""
        import mss
        _ensure_dpi_aware()
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}_post.png"
        out = self._screenshot_dir / fname

        def _do() -> None:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                grab = sct.grab(mon)
                mss.tools.to_png(grab.rgb, grab.size, output=str(out))
        await asyncio.to_thread(_do)
        return str(out)

    async def _maybe_capture_after(
        self, result: ToolResult, call: ToolCall, t0: float,
        before_hash: str | None = None,
    ) -> ToolResult:
        """If capture_after is enabled, attach a post-action screenshot."""
        try:
            path = await self._quick_capture()
        except Exception as exc:  # noqa: BLE001
            content = json.loads(result.content) if result.content else {}
            content["capture_after_warning"] = f"post-capture failed: {exc}"
            return ToolResult(
                call_id=call.id, ok=result.ok,
                content=json.dumps(content, ensure_ascii=False),
                error=result.error,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                metadata=result.metadata,
            )
        content = json.loads(result.content) if result.content else {}
        after_hash = _sha256_file(path)
        if before_hash and after_hash:
            visual_changed = before_hash != after_hash
            content["visual_changed"] = visual_changed
            self._no_change_streak = 0 if visual_changed else self._no_change_streak + 1
            content["no_change_streak"] = self._no_change_streak
        if after_hash:
            self._last_screen_hash = after_hash
        content["capture_after"] = True
        content["post_capture_path"] = path
        content["coordinate_space"] = self._coordinate_space()
        if self._no_change_streak >= 2:
            content["strategy_switch_required"] = True
            content["recoveries"] = self._computer_recoveries("no_visual_change")
        metadata = dict(result.metadata or {})
        metadata["attach_image"] = path
        return ToolResult(
            call_id=call.id, ok=result.ok,
            content=json.dumps(content, ensure_ascii=False),
            error=result.error,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata=metadata,
        )

    async def _screen_hash_snapshot(self) -> str | None:
        try:
            path = await self._quick_capture()
        except Exception:  # noqa: BLE001
            return self._last_screen_hash
        digest = _sha256_file(path)
        if digest:
            self._last_screen_hash = digest
        return digest

    async def _observe(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        include_screenshot = bool(args.get("include_screenshot", True))
        include_ocr = bool(args.get("include_ocr", True))
        include_uia = bool(args.get("include_uia", True))
        include_action_log = bool(args.get("include_action_log", True))

        observation: dict[str, Any] = {
            "kind": "ScreenObservation",
            "platform": platform.system(),
            "coordinate_space": self._coordinate_space(),
            "active_window": {
                "hwnd": self._active_hwnd,
                "pid": self._active_pid,
                "title": self._last_window_title,
            },
            "no_change_streak": self._no_change_streak,
            "recoveries": self._computer_recoveries(
                "no_visual_change" if self._no_change_streak >= 2 else "observe"
            ),
            "recommended_next_actions": self._computer_next_actions(),
        }

        if include_screenshot:
            try:
                path = await self._quick_capture()
                digest = _sha256_file(path)
                if digest:
                    self._last_screen_hash = digest
                observation["screenshot"] = {
                    "path": path,
                    "sha256": digest,
                }
            except Exception as exc:  # noqa: BLE001
                observation["screenshot_error"] = str(exc)

        size_result = await self._screen_size(call, t0)
        if size_result.ok:
            observation["screen_size"] = _json_content(size_result.content)
        else:
            observation["screen_size_error"] = size_result.error

        cursor_result = await self._cursor_position(call, t0)
        if cursor_result.ok:
            observation["cursor"] = _json_content(cursor_result.content)
        else:
            observation["cursor_error"] = cursor_result.error

        windows_result = await self._window_list(call, t0, args)
        if windows_result.ok:
            observation["windows"] = _json_content(windows_result.content)
        else:
            observation["windows_error"] = windows_result.error

        if include_ocr:
            try:
                ocr_result = await self._screen_ocr(call, t0, args)
                if ocr_result.ok:
                    observation["ocr"] = _json_content(ocr_result.content)
                else:
                    observation["ocr_error"] = ocr_result.error
            except Exception as exc:  # noqa: BLE001
                observation["ocr_error"] = str(exc)

        if include_uia:
            try:
                uia_result = await self._ui_inspect(call, t0, args)
                if uia_result.ok:
                    observation["uia"] = _json_content(uia_result.content)
                else:
                    observation["uia_error"] = uia_result.error
            except Exception as exc:  # noqa: BLE001
                observation["uia_error"] = str(exc)

        if include_action_log:
            observation["action_log"] = list(self._action_log[-30:])

        self._last_observation = observation
        metadata: dict[str, Any] = {"computer_observation": observation}
        shot = observation.get("screenshot")
        if isinstance(shot, dict) and shot.get("path"):
            metadata["attach_image"] = shot["path"]
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=json.dumps(observation, ensure_ascii=False),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata=metadata,
        )

    async def _finalize_runtime_result(
        self, call: ToolCall, t0: float, action: str, result: ToolResult,
    ) -> ToolResult:
        self._record_computer_action(action, result)
        metadata = dict(result.metadata or {})
        if not result.ok:
            metadata.setdefault("recoveries", self._computer_recoveries(action))
        metadata.setdefault("coordinate_space", self._coordinate_space())
        return ToolResult(
            call_id=result.call_id,
            ok=result.ok,
            content=result.content,
            error=result.error,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            side_effects=result.side_effects,
            schema_version=result.schema_version,
            metadata=metadata,
        )

    def _record_computer_action(self, action: str, result: ToolResult) -> None:
        self._action_log.append({
            "ts": time.time(),
            "action": action,
            "ok": bool(result.ok),
            "error": result.error,
            "no_change_streak": self._no_change_streak,
        })
        del self._action_log[:-120]

    def _coordinate_space(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "screen_coordinate_origin": "top_left",
            "coordinate_unit": "physical_pixel_after_dpi_awareness",
            "screenshot_to_click_scale": [1.0, 1.0],
            "window_relative_coordinates": False,
        }
        try:
            pg = self._require_pyautogui()
            size = pg.size()
            info["pyautogui_size"] = [int(size[0]), int(size[1])]
        except Exception as exc:  # noqa: BLE001
            info["pyautogui_error"] = str(exc)
        return info

    def _computer_next_actions(self) -> list[dict[str, str]]:
        if self._no_change_streak >= 2:
            return [
                {"action": "ui_inspect/ui_click", "reason": "连续动作无画面变化，优先换 UIA 控件路线。"},
                {"action": "ocr/find_text", "reason": "UIA 不可用时改用 OCR 文本定位。"},
                {"action": "ask_user", "reason": "仍无变化时让用户确认窗口/权限/目标位置。"},
            ]
        return [
            {"action": "ui_inspect", "reason": "能用控件树就不用裸坐标。"},
            {"action": "capture(mode='som')", "reason": "需要视觉定位时先取 SOM 元素索引。"},
        ]

    def _computer_recoveries(self, state_or_action: str) -> list[dict[str, str]]:
        if state_or_action == "no_visual_change":
            return [
                {"route": "UIA", "reason": "同一动作没有改变屏幕，改用控件树定位。"},
                {"route": "OCR", "reason": "控件树不可用时按屏幕文字定位。"},
                {"route": "SOM", "reason": "文字不稳定时重新截图并用编号元素点击。"},
                {"route": "ask_user", "reason": "连续失败后确认权限、窗口焦点或目标是否存在。"},
            ]
        if state_or_action in {"click", "double_click", "right_click", "click_text", "click_image"}:
            return [
                {"route": "observe", "reason": "先确认窗口、坐标、缩放和画面是否变化。"},
                {"route": "ui_inspect/ui_click", "reason": "优先使用原生控件模式。"},
                {"route": "capture(mode='som')", "reason": "刷新元素编号后再点。"},
            ]
        if state_or_action in {"type", "key"}:
            return [
                {"route": "focus_window", "reason": "输入前确认焦点窗口。"},
                {"route": "observe", "reason": "输入后检查光标位置与文本变化。"},
            ]
        return [{"route": "observe", "reason": "重新汇总桌面状态后再选择路线。"}]

    async def _update_sticky_window(self) -> None:
        """Record foreground window into sticky state (Windows only)."""
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                self._active_hwnd = int(hwnd)
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                self._active_pid = int(pid.value)
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    self._last_window_title = buf.value
                else:
                    self._last_window_title = ""
        except Exception:  # noqa: BLE001
            pass

    def _build_som_overlay(self, img_path: str, elements: list[dict]) -> str:
        """Draw numbered circles on screenshot for SOM mode.

        Each element gets a 1-indexed numbered badge in electric lime
        (#E5FF00) with a semi-transparent dark background for readability.
        """
        from PIL import Image, ImageDraw, ImageFont
        base = Image.open(img_path).convert("RGBA")
        # Overlay layer for alpha-blended rectangle highlights
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 14)
            except Exception:
                font = ImageFont.load_default()
        # Electric lime (#E5FF00) — deep-space theme
        LIME = (229, 255, 0, 255)
        LIME_FILL = (229, 255, 0, 40)
        DARK_BG = (0, 0, 0, 180)
        for i, el in enumerate(elements, start=1):
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x, y, w, h = bbox
            if w <= 0 or h <= 0:
                continue
            # Semi-transparent rectangle highlight on overlay layer
            draw_overlay.rectangle(
                [x, y, x + w, y + h],
                outline=LIME, fill=LIME_FILL, width=2,
            )
        # Composite highlights onto base image
        img = Image.alpha_composite(base, overlay)
        draw = ImageDraw.Draw(img)
        # Draw numbered circles on top of composited image
        for i, el in enumerate(elements, start=1):
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x, y, w, h = bbox
            if w <= 0 or h <= 0:
                continue
            radius = max(10, min(16, min(w, h) // 3))
            cx = x + radius
            cy = y + radius
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=DARK_BG, outline=LIME, width=2,
            )
            text = str(i)
            try:
                tb = draw.textbbox((0, 0), text, font=font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
            except Exception:
                tw, th = 8, 8
            tx = cx - tw // 2
            ty = cy - th // 2
            draw.text((tx, ty), text, fill=LIME, font=font)
        overlay_path = str(Path(img_path).with_suffix(".som.png"))
        img.save(overlay_path)
        return overlay_path

    def _resize_screenshot(self, img_path: str, max_w: int = 1920, max_h: int = 1080) -> tuple[str, float]:
        """Resize screenshot if it exceeds max dimensions. Returns (path, scale)."""
        from PIL import Image
        try:
            img = Image.open(img_path)
        except Exception:  # noqa: BLE001 — invalid image, skip resize
            return img_path, 1.0
        if img.width <= max_w and img.height <= max_h:
            img.close()
            return img_path, 1.0
        scale = min(max_w / img.width, max_h / img.height)
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(img_path)
        img.close()
        return img_path, scale

    async def _capture_with_som(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        """capture + ui_inspect + SOM overlay."""
        cap_args = {"monitor": args.get("monitor", 1)}
        cap_result = await self._screen_capture(call, t0, cap_args)
        if not cap_result.ok:
            return cap_result
        cap_data = json.loads(cap_result.content)
        img_path = cap_data["path"]

        # Resize screenshot to avoid context explosion
        img_path, scale = await asyncio.to_thread(self._resize_screenshot, img_path)

        inspect_args = {
            "window_title": args.get("window_title", ""),
            "max_depth": args.get("max_depth", 6),
            "control_type": args.get("control_type", ""),
            "name_contains": args.get("name_contains", ""),
            "max_elements": _clamp(int(args.get("max_elements", 50)), 1, 200),
        }
        inspect_result = await self._ui_inspect(call, t0, inspect_args)
        if not inspect_result.ok:
            elements: list[dict] = []
        else:
            inspect_data = json.loads(inspect_result.content)
            elements = inspect_data.get("elements", [])

        # Add 1-based index and scale bboxes for overlay drawing
        for idx, el in enumerate(elements, start=1):
            el["index"] = idx

        overlay_elements = []
        for el in elements:
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) == 4 and scale != 1.0:
                overlay_elements.append({
                    **el,
                    "bbox": [
                        int(bbox[0] * scale),
                        int(bbox[1] * scale),
                        int(bbox[2] * scale),
                        int(bbox[3] * scale),
                    ],
                })
            else:
                overlay_elements.append(el)

        try:
            overlay_path = self._build_som_overlay(img_path, overlay_elements)
        except Exception as exc:  # noqa: BLE001
            overlay_path = img_path
            # Do NOT wipe elements when overlay draw fails; the agent still
            # needs the element list even if the image overlay couldn't be drawn.
            pass

        self._last_som_elements = elements
        self._last_som_overlay_path = overlay_path

        result = {
            "path": overlay_path,
            "original_path": cap_data["path"],
            "size": cap_data.get("size"),
            "elements": elements,
            "mode": "som",
            "element_count": len(elements),
        }
        if scale != 1.0:
            result["scale"] = round(scale, 4)
        return ToolResult(
            call_id=call.id, ok=True,
            content=json.dumps(result, ensure_ascii=False),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            metadata={"attach_image": overlay_path},
        )

    async def _capture_fallback_text(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        """Non-vision fallback: capture → OCR → return text description."""
        cap_result = await self._screen_capture(call, t0, args)
        if not cap_result.ok:
            return cap_result
        cap_data = json.loads(cap_result.content)
        img_path = cap_data["path"]
        try:
            blocks = await asyncio.to_thread(
                _run_ocr_full_pipeline, None,
                float(args.get("min_confidence", 0.5)),
            )
        except _NoOCREngineError as exc:
            return _fail(call, t0, str(exc))
        except Exception as exc:
            return _fail(call, t0, f"OCR fallback failed: {exc}")
        texts = [b["text"] for b in blocks]
        result = {
            "text_description": "\n".join(texts),
            "blocks": blocks,
            "path": img_path,
            "size": cap_data.get("size"),
            "vision_attached": False,
            "fallback": "non_vision_ocr",
        }
        return _ok(call, t0, json.dumps(result, ensure_ascii=False))

    async def _computer_use(self, call: ToolCall, t0: float, args: dict) -> ToolResult:
        action = str(args.get("action", "")).strip().lower()
        if not action:
            return _fail(call, t0, "action parameter is required for computer_use")

        block_reason = self._danger_check(action, args)
        if block_reason:
            return _fail(call, t0, f"BLOCKED: {block_reason}")

        capture_after = args.get("capture_after", action in _MUTATING_ACTIONS)
        if isinstance(capture_after, str):
            capture_after = capture_after.lower() in ("true", "1", "yes")
        capture_after = bool(capture_after)
        before_hash: str | None = None
        if capture_after and action in _MUTATING_ACTIONS:
            before_hash = await self._screen_hash_snapshot()

        # Resolve element index -> coordinate (1-indexed, matched by `index` field)
        element_idx = args.get("element")
        if element_idx is not None:
            try:
                element_idx = int(element_idx)
            except (TypeError, ValueError):
                return _fail(call, t0, "element must be an integer")
            if not self._last_som_elements:
                return _fail(
                    call, t0,
                    "先调用 capture(mode='som') 以获取元素索引",
                )
            el = None
            for e in self._last_som_elements:
                if e.get("index") == element_idx:
                    el = e
                    break
            if el is None:
                return _fail(
                    call, t0,
                    f"element {element_idx} 不存在 "
                    f"(当前有 {len(self._last_som_elements)} 个 SOM 元素)",
                )
            bbox = el.get("bbox", [0, 0, 0, 0])
            if len(bbox) == 4:
                cx = bbox[0] + bbox[2] // 2
                cy = bbox[1] + bbox[3] // 2
                args = {**args, "x": cx, "y": cy}

        # Resolve coordinate array -> x, y
        coordinate = args.get("coordinate")
        if coordinate is not None and isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
            try:
                x = int(coordinate[0])
                y = int(coordinate[1])
                args = {**args, "x": x, "y": y}
            except (TypeError, ValueError):
                return _fail(call, t0, "coordinate must be [x, y] integers")

        result: ToolResult | None = None

        if action == "observe":
            result = await self._observe(call, t0, args)
        elif action == "capture":
            mode = args.get("mode", "vision")
            if mode == "som":
                result = await self._capture_with_som(call, t0, args)
            else:
                if not args.get("vision", True):
                    result = await self._capture_fallback_text(call, t0, args)
                else:
                    result = await self._screen_capture(call, t0, args)
        elif action == "screen_size":
            result = await self._screen_size(call, t0)
        elif action == "cursor_position":
            result = await self._cursor_position(call, t0)
        elif action in ("move", "mouse_move"):
            result = await self._mouse_move(call, t0, args)
        elif action in ("click", "mouse_click", "double_click", "right_click"):
            click_args = dict(args)
            if action == "double_click":
                click_args["count"] = 2
            elif action == "right_click":
                click_args["button"] = "right"
            result = await self._mouse_click(call, t0, click_args)
        elif action in ("drag", "mouse_drag"):
            result = await self._mouse_drag(call, t0, args)
        elif action in ("scroll", "mouse_scroll"):
            result = await self._mouse_scroll(call, t0, args)
        elif action in ("type", "keyboard_type"):
            result = await self._keyboard_type(call, t0, args)
        elif action in ("key", "keyboard_press"):
            result = await self._keyboard_press(call, t0, args)
        elif action in ("list_windows", "window_list"):
            result = await self._window_list(call, t0, args)
        elif action in ("focus_window", "window_focus"):
            result = await self._window_focus(call, t0, args)
            if result and result.ok:
                await self._update_sticky_window()
        elif action == "ocr":
            result = await self._screen_ocr(call, t0, args)
        elif action in ("find_text", "find_on_screen"):
            result = await self._find_on_screen(call, t0, args)
        elif action in ("click_text", "click_on_text"):
            result = await self._click_on_text(call, t0, args)
        elif action == "wait_for_text":
            result = await self._wait_for_text(call, t0, args)
        elif action in ("region_capture", "screen_region_capture"):
            result = await self._screen_region_capture(call, t0, args)
        elif action in ("find_image", "find_image_on_screen"):
            result = await self._find_image_on_screen(call, t0, args)
        elif action in ("click_image", "click_on_image"):
            result = await self._click_on_image(call, t0, args)
        elif action == "scroll_to_text":
            result = await self._scroll_to_text(call, t0, args)
        elif action in ("ui_inspect",):
            result = await self._ui_inspect(call, t0, args)
        elif action in ("ui_click",):
            result = await self._ui_click(call, t0, args)
        elif action in ("gui_send_chat",):
            result = await self._gui_send_chat(call, t0, args)
        else:
            return _fail(call, t0, f"unknown action: {action!r}")

        # Post-action capture for mutating actions (except capture itself)
        if capture_after and result and result.ok and action not in _READ_ONLY_ACTIONS:
            result = await self._maybe_capture_after(result, call, t0, before_hash)

        # Update sticky window after capture
        if action == "capture" and result and result.ok:
            await self._update_sticky_window()

        if result is not None:
            return await self._finalize_runtime_result(call, t0, action, result)
        return _fail(call, t0, f"unknown action: {action!r}")


    # ── pyautogui import gate ──────────────────────────────────────

    def _require_pyautogui(self):
        """Return the pyautogui module or raise ImportError with a
        clear install hint. Called from every mouse/keyboard tool."""
        _ensure_dpi_aware()  # Phase 9 M2.1: 坐标系与截图物理像素对齐
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
        _ensure_dpi_aware()  # Phase 9 M2.1: 截图前就绪,坐标系一致
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
        # Phase 9 M2.1: 回报 pyautogui 的坐标空间。DPI 感知开启后两边
        # 通常一致 (click_scale=[1,1]);若不一致(感知设置太晚/特殊
        # 多屏),模型需把截图坐标乘 click_scale 再交给 mouse_* 工具。
        # 仅主屏截图时才有意义(虚拟屏 union 的 offset 另说),其余
        # monitor 不回报,避免误导。
        if monitor_idx == 1:
            try:
                pg = self._require_pyautogui()
                pg_w, pg_h = pg.size()
                result["pyautogui_size"] = [int(pg_w), int(pg_h)]
                if int(size[0]) and int(size[1]):
                    result["click_scale"] = [
                        round(int(pg_w) / int(size[0]), 4),
                        round(int(pg_h) / int(size[1]), 4),
                    ]
            except Exception:  # noqa: BLE001 — pyautogui 缺失不碍截图
                pass
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
            payload: dict[str, Any] = {
                "x": x, "y": y, "button": button, "count": count,
            }
        else:
            await asyncio.to_thread(pg.click, button=button, clicks=count)
            pos = pg.position()
            payload = {
                "x": int(pos[0]), "y": int(pos[1]),
                "button": button, "count": count,
            }

        # Optional pixel-level verification
        if args.get("verify_pixel") and x is not None and y is not None:
            pixel_verify = await self._verify_pixel_after_click(x, y)
            if pixel_verify is not None:
                payload.update(pixel_verify)

        verify = await self._verify_after_action(args)
        if verify is not None:
            payload.update(verify)
            if not verify.get("verified") and x is not None and y is not None:
                # Neighbourhood retry: 4 diagonal offsets ±3 px
                offsets = [(3, 3), (3, -3), (-3, 3), (-3, -3)]
                for n, (dx, dy) in enumerate(offsets, start=1):
                    rx, ry = x + dx, y + dy
                    try:
                        await asyncio.to_thread(
                            pg.click, x=rx, y=ry, button=button, clicks=count,
                        )
                    except Exception as exc:  # noqa: BLE001
                        continue
                    retry_verify = await self._verify_after_action(args)
                    if retry_verify and retry_verify.get("verified"):
                        payload.update(retry_verify)
                        payload.update({
                            "retried": True,
                            "retries": n,
                            "x": rx,
                            "y": ry,
                        })
                        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))
                # All 4 retries failed — mark final failure
                payload["retried"] = True
                payload["retries"] = len(offsets)
                return _ok(call, t0, json.dumps(payload, ensure_ascii=False))
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

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
            # 2026-05-13 r4: pygetwindow.activate() returns ok on
            # Windows even when the OS silently denied the focus
            # change (focus-stealing prevention). We use the lower-
            # level SetForegroundWindow path via ctypes, with the
            # ALT-key trick that bypasses the restriction (works
            # because alt-tab implicitly grants the calling thread
            # foreground rights). Falls back to pygetwindow if win32
            # APIs are unavailable.
            _activated_via = "pygetwindow"
            activation_warning: str | None = None
            try:
                _activated_via = _force_foreground(chosen)
            except Exception as exc:  # noqa: BLE001 — fall back
                activation_warning = (
                    f"win32 force-foreground failed ({exc}); "
                    "falling back to pygetwindow.activate"
                )
                try:
                    chosen.activate()
                except Exception as exc2:  # noqa: BLE001
                    raise RuntimeError(
                        f"activate failed for {chosen.title!r}: {exc2}",
                    )
            # Verify it actually came to front. GetForegroundWindow
            # is the ground truth.
            is_frontmost = False
            try:
                import ctypes
                hwnd_fg = ctypes.windll.user32.GetForegroundWindow()
                hwnd_target = getattr(chosen, "_hWnd", None)
                if hwnd_target is None:
                    # pygetwindow's Win32Window stores hwnd here
                    hwnd_target = int(getattr(chosen, "hWnd", 0))
                is_frontmost = (hwnd_fg == int(hwnd_target))
            except Exception:  # noqa: BLE001
                pass
            payload = {
                "title": (chosen.title or "")[:160],
                "bbox": [
                    int(getattr(chosen, "left", 0)),
                    int(getattr(chosen, "top", 0)),
                    int(getattr(chosen, "width", 0)),
                    int(getattr(chosen, "height", 0)),
                ],
                "activated_via": _activated_via,
                "is_frontmost": is_frontmost,
            }
            if activation_warning:
                payload["warning"] = activation_warning
            return payload

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

        payload: dict[str, Any] = {
            "clicked": True,
            "x": x, "y": y,
            "button": button,
            "count": count,
            "match_text": find_payload["match_text"],
            "confidence": find_payload["confidence"],
            "bbox": find_payload["bbox"],
        }
        verify = await self._verify_after_action(args)
        if verify is not None:
            payload.update(verify)
            if not verify.get("verified"):
                # 5-point candidate retry: centre + 4 corners
                bbox = find_payload.get("bbox", [x, y, 0, 0])
                bx, by, bw, bh = bbox
                candidates = [
                    (x, y),                    # centre
                    (bx, by),                  # top-left
                    (bx + bw, by + bh),        # bottom-right
                    (bx + bw, by),             # top-right
                    (bx, by + bh),             # bottom-left
                ]
                for n, (cx, cy) in enumerate(candidates[1:], start=1):
                    try:
                        await asyncio.to_thread(
                            pg.click, x=cx, y=cy, button=button, clicks=count,
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    retry_v = await self._verify_after_action(args)
                    if retry_v and retry_v.get("verified"):
                        payload.update(retry_v)
                        payload.update({
                            "retried": True,
                            "retries": n,
                            "x": cx,
                            "y": cy,
                        })
                        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))
                payload["retried"] = True
                payload["retries"] = len(candidates) - 1
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

    # Phase 9 M2.3: 动作后验证。点击类工具带 ``verify_text`` 时,动作
    # 完成后轮询 OCR 等该文本出现 —— 把"点了但没生效"从静默继续变成
    # 显式信号(verified: false + 屏上实际读到了什么),agent 据此重试
    # 或换策略,而不是带着错误假设往下走。
    async def _verify_after_action(self, args: dict) -> dict[str, Any] | None:
        """Poll OCR for ``args["verify_text"]``; None when not requested.

        Returns a dict to merge into the tool result payload:
        ``verified`` true/false (+ diagnostics), or ``verify_skipped``
        when no OCR engine is available (verification degrades, the
        action result itself is unaffected).
        """
        text = args.get("verify_text")
        if not isinstance(text, str) or not text.strip():
            return None
        timeout_s = _clamp(float(args.get("verify_timeout_s", 5.0)), 0.5, 30.0)
        region = args.get("verify_region")
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
                return {"verified": None, "verify_skipped": str(exc)}
            except Exception:  # noqa: BLE001 — transient OCR error, retry
                blocks = []
            if _match_text_in_blocks(blocks, text, exact=False):
                return {
                    "verified": True,
                    "verify_text": text,
                    "verify_attempts": attempts,
                }
            await asyncio.sleep(0.6)
        return {
            "verified": False,
            "verify_text": text,
            "verify_attempts": attempts,
            "verify_timeout_s": timeout_s,
            "verify_hint": (
                "动作已执行但预期文本未出现 — 不要假设成功。"
                "重新 screen_capture 看实际状态,再决定重试还是换路径。"
            ),
            "sample_blocks_last_poll": [
                {"text": b["text"], "confidence": b["confidence"]}
                for b in last_blocks[:10]
            ],
        }

    async def _verify_pixel_after_click(
        self, x: int, y: int, timeout_s: float = 2.0,
    ) -> dict[str, Any] | None:
        """点击前后比较目标像素颜色变化。如果颜色没变，可能点击未触发UI更新。"""
        try:
            import numpy as np
        except ImportError:
            return None
        try:
            pg = self._require_pyautogui()
        except Exception:  # noqa: BLE001
            return None

        def _sample() -> tuple[int, int, int] | None:
            try:
                im = pg.screenshot(region=(max(0, x - 1), max(0, y - 1), 3, 3))
                arr = np.array(im)
                # centre pixel of the 3x3 grab
                return (int(arr[1, 1, 0]), int(arr[1, 1, 1]), int(arr[1, 1, 2]))
            except Exception:  # noqa: BLE001
                return None

        before = await asyncio.to_thread(_sample)
        if before is None:
            return None
        await asyncio.sleep(0.3)
        after = await asyncio.to_thread(_sample)
        if after is None:
            return None
        diff = sum(abs(a - b) for a, b in zip(before, after))
        return {
            "pixel_changed": diff > 30,  # threshold ~10/255 per channel
            "pixel_diff": diff,
            "pixel_before": list(before),
            "pixel_after": list(after),
        }

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
            orig_th, orig_tw = template.shape[:2]
            # Grab full screen / region as BGR ndarray
            screen, (ox, oy) = _grab_for_ocr(region)

            best_result: dict[str, Any] | None = None
            best_max_val = -1.0
            scales = [0.75, 1.0, 1.25]
            scale_tried: list[float] = []

            for scale in scales:
                scale_tried.append(scale)
                if scale == 1.0:
                    scaled_template = template
                else:
                    new_w = max(1, int(round(orig_tw * scale)))
                    new_h = max(1, int(round(orig_th * scale)))
                    scaled_template = cv2.resize(template, (new_w, new_h))
                sth, stw = scaled_template.shape[:2]
                if sth > screen.shape[0] or stw > screen.shape[1]:
                    continue
                res = cv2.matchTemplate(screen, scaled_template, cv2.TM_CCOEFF_NORMED)
                _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
                if max_val > best_max_val:
                    best_max_val = max_val
                    best_result = {
                        "scale": scale,
                        "max_val": max_val,
                        "max_loc": max_loc,
                        "tw": stw,
                        "th": sth,
                    }

            if best_result is None:
                return {
                    "found": False,
                    "best_confidence": 0.0,
                    "threshold": confidence,
                    "scale_tried": scale_tried,
                }

            if best_max_val < confidence:
                return {
                    "found": False,
                    "best_confidence": round(float(best_max_val), 4),
                    "threshold": confidence,
                    "scale_tried": scale_tried,
                }

            x0, y0 = int(best_result["max_loc"][0]) + ox, int(best_result["max_loc"][1]) + oy
            tw = best_result["tw"]
            th = best_result["th"]
            return {
                "found": True,
                "x": x0 + tw // 2,
                "y": y0 + th // 2,
                "bbox": [x0, y0, tw, th],
                "confidence": round(float(best_max_val), 4),
                "scale_used": best_result["scale"],
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

        payload: dict[str, Any] = {
            "clicked": True, "x": x, "y": y,
            "button": button, "count": count,
            "template": args.get("template_path"),
            "confidence": fp["confidence"],
            "bbox": fp["bbox"],
        }
        verify = await self._verify_after_action(args)
        if verify is not None:
            payload.update(verify)
            if not verify.get("verified"):
                # 5-point candidate retry: centre + 4 corners
                bbox = fp.get("bbox", [x, y, 0, 0])
                bx, by, bw, bh = bbox
                candidates = [
                    (x, y),                    # centre
                    (bx, by),                  # top-left
                    (bx + bw, by + bh),        # bottom-right
                    (bx + bw, by),             # top-right
                    (bx, by + bh),             # bottom-left
                ]
                for n, (cx, cy) in enumerate(candidates[1:], start=1):
                    try:
                        await asyncio.to_thread(
                            pg.click, x=cx, y=cy, button=button, clicks=count,
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    retry_v = await self._verify_after_action(args)
                    if retry_v and retry_v.get("verified"):
                        payload.update(retry_v)
                        payload.update({
                            "retried": True,
                            "retries": n,
                            "x": cx,
                            "y": cy,
                        })
                        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))
                payload["retried"] = True
                payload["retries"] = len(candidates) - 1
        return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

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
        max_elements = _clamp(int(args.get("max_elements", 100)), 1, 200)

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
                    if depth > max_depth or len(elements) >= max_elements:
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
                            if len(elements) >= max_elements:
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

    # ── Atomic chat compose+send (2026-05-12 r3) ──────────────────

    @staticmethod
    def _chat_header_bbox(target_bbox: list[int] | None) -> list[int] | None:
        """Return the [x, y, w, h] of the chat-header OCR strip.

        WeChat 4.x layout (validated against user screenshot
        2026-05-13):
        * Top ~40 px: window chrome / title bar — empty grey, no text.
          DO NOT include this — OCR returns nothing.
        * Below chrome ~50-60 px: conversation header with chat name
          (e.g. "魔丸(5)") on the left, action icons on the right.
        * Below that: messages.

        We aim the OCR strip at the chat header band only:
        * y starts at wy + 40 (skip chrome)
        * h = 60 (catch one band of header text)
        * x starts at right_pane_x (skip chat list)
        * w = 40% of right pane (skip action icons on right)
        """
        if target_bbox is None or len(target_bbox) != 4:
            return None
        wx, wy, ww, wh = target_bbox
        if ww <= 0 or wh <= 0:
            return None
        # Right pane starts ~30% into the window width.
        right_pane_x = wx + ww // 3
        right_pane_w = ww - ww // 3
        # Skip the window chrome / title bar (~40 px) at top.
        chrome_offset = 40
        # Narrow strip: left 40% of right pane is where the title is.
        title_strip_w = max(120, int(right_pane_w * 0.4))
        header_h = min(60, max(40, wh // 10))
        return [
            right_pane_x,
            wy + chrome_offset,
            title_strip_w,
            header_h,
        ]

    async def _gui_send_chat(
        self, call: ToolCall, t0: float, args: dict,
    ) -> ToolResult:
        """Compose-and-send a chat message in one tool call.

        Failure modes we explicitly handle:

        * No focused window after window_focus → return error, don't
          guess coordinates blindly.
        * Empty text → reject (model probably meant something else).
        * Input box heuristic miss → caller can retry with explicit
          input_bbox or fall back to manual click+type+enter.
        """
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(call, t0, "text (non-empty string) required")
        if len(text) > _MAX_TYPE_LEN:
            return _fail(
                call, t0,
                f"text > {_MAX_TYPE_LEN} chars — split into multiple "
                "gui_send_chat calls",
            )
        window_title = args.get("window_title")
        explicit_bbox = args.get("input_bbox")
        nav_chat_name = args.get("nav_chat_name")
        if nav_chat_name is not None and not isinstance(nav_chat_name, str):
            return _fail(call, t0, "nav_chat_name must be a string")
        verify_chat_title = args.get("verify_chat_title")
        if verify_chat_title is not None and not isinstance(verify_chat_title, str):
            return _fail(call, t0, "verify_chat_title must be a string")
        press_after = args.get("press_after", "enter")
        if not isinstance(press_after, str):
            press_after = "enter"
        # Default ON — the verify screenshot is cheap insurance and
        # the agent needs it to actually trust the send happened.
        confirm_screenshot = bool(args.get("confirm_screenshot", True))

        # ── Step 1: focus target window ──
        target_bbox: list[int] | None = None
        chosen_proc_name: str = ""
        if isinstance(window_title, str) and window_title.strip():
            try:
                import pygetwindow as gw  # type: ignore  # noqa: F401
            except ImportError:
                return _fail(
                    call, t0,
                    "gui_send_chat needs ``pygetwindow``. "
                    "pip install pygetwindow",
                )
            substring = window_title.strip()
            try:
                chosen = await asyncio.to_thread(
                    _pick_chat_window, substring,
                )
            except Exception as exc:  # noqa: BLE001
                return _fail(call, t0, f"pick-chat-window failed: {exc}")
            if chosen is None:
                return _fail(
                    call, t0,
                    f"no chat-app window with title containing "
                    f"{window_title!r} (after filtering out browser "
                    f"tabs and the WeChat mini-program auxiliary "
                    f"window). Open the app first, then retry.",
                )
            chosen_proc_name = _window_process_name(chosen)
            try:
                if bool(getattr(chosen, "isMinimized", False)):
                    chosen.restore()
                # Use the win32-foreground bypass — pygetwindow.activate
                # alone silently failed against Windows 11 focus-
                # stealing protection (real bug seen with WeChat hiding
                # behind Claude / browser). _force_foreground PINS the
                # window HWND_TOPMOST so other topmost windows (Claude
                # Code's task panel) can't obscure it. Pin stays until
                # the next tool invocation releases it (see invoke()).
                _force_foreground(chosen)
                self._pending_topmost_release = chosen
            except Exception:  # noqa: BLE001 — Windows occasionally raises
                try:
                    chosen.activate()
                except Exception:  # noqa: BLE001
                    pass
            # Give Windows + WeChat time to render the activated state
            # before we OCR. 0.4s was empirically not enough during
            # the test — the chat header was OCR'd before WeChat
            # actually finished its paint cycle.
            await asyncio.sleep(0.8)
            try:
                target_bbox = [
                    int(getattr(chosen, "left", 0)),
                    int(getattr(chosen, "top", 0)),
                    int(getattr(chosen, "width", 0)),
                    int(getattr(chosen, "height", 0)),
                ]
            except Exception:  # noqa: BLE001
                target_bbox = None

        # ── Step 1.25: navigate to target chat (if nav_chat_name set) ──
        # Strategy: OCR-find-and-click the chat name in the left-pane
        # chat list. This is THE one path that has empirically worked
        # in real WeChat / 飞书 / Slack tests — focusing the window
        # then clicking an OCR'd text block. We previously tried
        # Ctrl+F to use WeChat's search but that fires the BROWSER's
        # find-in-page when focus is even slightly off (real bug seen
        # in the e2e trace).
        nav_clicked: list[int] | None = None
        nav_strategy: str | None = None
        # Allow skip when header already shows wanted chat — saves
        # a navigation hop on re-tries.
        skip_nav_because_already_there = False
        if nav_chat_name and nav_chat_name.strip():
            wanted_chat = nav_chat_name.strip()
            if target_bbox is None or target_bbox[2] <= 0:
                return _fail(
                    call, t0,
                    "nav_chat_name set but no window bbox known — "
                    "pass window_title so the chat list can be located",
                )
            # Pre-check: if verify_chat_title is also set AND the
            # current chat-header OCR already matches, skip the
            # nav click entirely (the user / agent may have already
            # opened the right chat). This is the fast-path for
            # idempotent send.
            if verify_chat_title and verify_chat_title.strip():
                wanted_v = verify_chat_title.strip()
                pre_header_bbox = self._chat_header_bbox(target_bbox)
                if pre_header_bbox is not None:
                    try:
                        pre_blocks = await asyncio.to_thread(
                            _run_ocr_full_pipeline,
                            pre_header_bbox, 0.5,
                        )
                        pre_text = " ".join(
                            b.get("text", "")
                            for b in (pre_blocks or [])
                        )
                        plow = pre_text.casefold()
                        wlow = wanted_v.casefold()
                        if wlow in plow:
                            skip_nav_because_already_there = True
                        else:
                            w_chars = {c for c in wlow if not c.isspace()}
                            if w_chars and (
                                len(w_chars & set(plow)) / len(w_chars) >= 0.5
                            ):
                                skip_nav_because_already_there = True
                    except Exception:  # noqa: BLE001
                        pass

            if not skip_nav_because_already_there:
                try:
                    pg_nav = self._require_pyautogui()
                except ImportError as exc:
                    return _fail(call, t0, _pg_install_hint(exc))

                wx, wy, ww, wh = target_bbox
                chat_list_bbox = [wx, wy + 60, ww // 3, wh - 60]
                try:
                    blocks = await asyncio.to_thread(
                        _run_ocr_full_pipeline, chat_list_bbox, 0.5,
                    )
                except _NoOCREngineError as exc:
                    return _fail(call, t0, str(exc))
                except Exception as exc:  # noqa: BLE001
                    return _fail(
                        call, t0,
                        f"chat-list OCR failed: {type(exc).__name__}: {exc}",
                    )
                matches = _match_text_in_blocks(
                    blocks or [], wanted_chat, exact=False,
                )
                if matches:
                    top = matches[0]
                    nav_x = int(top["center"][0])
                    nav_y = int(top["center"][1])
                    try:
                        await asyncio.to_thread(pg_nav.click, nav_x, nav_y)
                    except Exception as exc:  # noqa: BLE001
                        return _fail(
                            call, t0,
                            f"nav click failed at ({nav_x}, {nav_y}): "
                            f"{type(exc).__name__}: {exc}",
                        )
                    nav_clicked = [nav_x, nav_y]
                    nav_strategy = "ocr_chat_list_click"
                    # Conversation pane needs time to load + re-render
                    # the chat header text. 0.6s was too short in
                    # practice; 1.2s gives reliable paint.
                    await asyncio.sleep(1.2)
                else:
                    # FALLBACK: chat is scrolled out of view. Use the
                    # app's search box. We OCR'd "Q 搜索" / "Search"
                    # near the top of chat list — click it, paste
                    # name, press Enter. This is robust against any
                    # number of chats in the list.
                    search_block = None
                    for b in (blocks or []):
                        text = (b.get("text", "") or "").strip()
                        # WeChat search box text: "Q搜索" / "Q 搜索";
                        # 飞书 / Slack: "Search"; QQ: "搜索"
                        low = text.casefold()
                        if (
                            "搜索" in low
                            or low == "search"
                            or low.startswith("search ")
                            or low.startswith("q搜索")
                            or low.startswith("q 搜索")
                        ):
                            search_block = b
                            break
                    if search_block is None:
                        return ToolResult(
                            call_id=call.id, ok=False, content=None,
                            error=(
                                f"nav_chat_name {wanted_chat!r} not in "
                                f"visible chat list AND no search box "
                                f"detected. OCR sample: "
                                f"{[b.get('text','')[:20] for b in (blocks or [])[:8]]}. "
                                f"If the app has hundreds of chats, "
                                f"scroll down with mouse_scroll until "
                                f"the chat is visible, then retry."
                            ),
                            latency_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    sx = int(search_block["center"][0])
                    sy = int(search_block["center"][1])
                    try:
                        await asyncio.to_thread(pg_nav.click, sx, sy)
                        await asyncio.sleep(0.3)
                        # Clear any prior content + paste new query.
                        await asyncio.to_thread(pg_nav.hotkey, "ctrl", "a")
                        await asyncio.sleep(0.1)
                        try:
                            import pyperclip  # type: ignore
                            saved_clip_search = None
                            try:
                                saved_clip_search = pyperclip.paste()
                            except Exception:  # noqa: BLE001
                                pass
                            await asyncio.to_thread(
                                pyperclip.copy, wanted_chat,
                            )
                            await asyncio.sleep(0.1)
                            await asyncio.to_thread(
                                pg_nav.hotkey, "ctrl", "v",
                            )
                            if saved_clip_search is not None:
                                try:
                                    await asyncio.to_thread(
                                        pyperclip.copy, saved_clip_search,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                        except ImportError:
                            await asyncio.to_thread(
                                pg_nav.write, wanted_chat, interval=0.02,
                            )
                        # WeChat shows a dropdown below the search box:
                        #   * top section "搜索网络结果" / 关键词建议
                        #   * "群聊" heading
                        #   * the actual group chats (with avatars)
                        # PRESSING ENTER selects the TOP entry which is
                        # the WEB-SEARCH, NOT the chat we want — sends
                        # the user to a search-engine page. Real bug
                        # demonstrated by user's screenshot 2026-05-13.
                        # Wait for dropdown to render, then OCR-find
                        # the "群聊" heading and click the first
                        # matching chat below it.
                        await asyncio.sleep(0.8)
                        # OCR the dropdown area — roughly below the
                        # search box, in the left ~1/3 of the window.
                        wx2, wy2, ww2, wh2 = target_bbox
                        dropdown_bbox = [
                            wx2,
                            sy + 25,           # just below search box
                            min(ww2 // 3 + 80, 520),
                            min(wh2 - (sy - wy2) - 25, 600),
                        ]
                        try:
                            drop_blocks = await asyncio.to_thread(
                                _run_ocr_full_pipeline,
                                dropdown_bbox, 0.4,
                            )
                        except Exception:  # noqa: BLE001
                            drop_blocks = []
                        # Find "群聊" heading y-coordinate.
                        group_section_y = None
                        for b in (drop_blocks or []):
                            text_b = (b.get("text", "") or "")
                            if "群聊" in text_b or "Group" in text_b:
                                group_section_y = b.get("center", [0, 0])[1]
                                break
                        if group_section_y is not None:
                            # Find chat matches BELOW the 群聊 heading.
                            below = [
                                b for b in drop_blocks
                                if b.get("center", [0, 0])[1]
                                > group_section_y
                            ]
                            chat_matches = _match_text_in_blocks(
                                below, wanted_chat, exact=False,
                            )
                            # Reject matches that look like other
                            # sections (网络结果 prefix etc).
                            chat_matches = [
                                m for m in chat_matches
                                if "网络" not in m.get("text", "")
                                and "搜索" not in m.get("text", "")
                            ]
                        else:
                            chat_matches = []
                        if chat_matches:
                            chat_m = chat_matches[0]
                            cx2 = int(chat_m["center"][0])
                            cy2 = int(chat_m["center"][1])
                            await asyncio.to_thread(pg_nav.click, cx2, cy2)
                            nav_clicked = [cx2, cy2]
                            nav_strategy = "search_dropdown_group_section"
                        else:
                            # No "群聊" section found in dropdown
                            # (maybe single-result or dropdown layout
                            # differs). Fall back to pressing Enter,
                            # accepting risk of web-search hit.
                            await asyncio.to_thread(pg_nav.press, "enter")
                            nav_clicked = [sx, sy]
                            nav_strategy = "search_box_enter_fallback"
                    except Exception as exc:  # noqa: BLE001
                        return _fail(
                            call, t0,
                            f"search-box navigation failed: "
                            f"{type(exc).__name__}: {exc}",
                        )
                    # Search-result open + conversation paint takes
                    # slightly longer than a direct chat-list click.
                    await asyncio.sleep(1.5)
            # Re-read window bbox — clicking a chat may shift the
            # window position on some systems (rare but observed).
            try:
                if isinstance(window_title, str) and window_title.strip():
                    re_chosen = await asyncio.to_thread(
                        _pick_chat_window, window_title.strip(),
                    )
                    if re_chosen is not None:
                        target_bbox = [
                            int(getattr(re_chosen, "left", 0)),
                            int(getattr(re_chosen, "top", 0)),
                            int(getattr(re_chosen, "width", 0)),
                            int(getattr(re_chosen, "height", 0)),
                        ]
            except Exception:  # noqa: BLE001
                pass

        # ── Step 1.5: OCR-verify the active chat header (anti-wrong-chat) ──
        # We OCR a narrow strip at the very top of the focused window
        # — roughly where chat apps render the conversation title —
        # and abort if the expected title substring isn't found. The
        # strip is small enough that OCR finishes in 1-3 s rather than
        # the 20-30 s a full-window scan takes.
        if verify_chat_title and verify_chat_title.strip():
            wanted = verify_chat_title.strip()
            header_bbox = self._chat_header_bbox(target_bbox)
            if header_bbox is None:
                return _fail(
                    call, t0,
                    "verify_chat_title set but no window bbox known — "
                    "pass window_title so the chat header strip can "
                    "be located",
                )
            try:
                header_blocks = await asyncio.to_thread(
                    _run_ocr_full_pipeline, header_bbox, 0.5,
                )
            except _NoOCREngineError as exc:
                return _fail(call, t0, str(exc))
            except Exception as exc:  # noqa: BLE001
                return _fail(
                    call, t0,
                    f"chat-header OCR failed: {type(exc).__name__}: {exc}",
                )
            header_text = " ".join(
                b.get("text", "") for b in (header_blocks or [])
            )
            # Tolerant chat-title match. RapidOCR on small Chinese
            # text routinely drops one character (e.g. "魔丸" → "魔"
            # only, or "魔丸(5)" → "(5)" only). Strict substring would
            # block legitimate sends; we accept the match when EITHER:
            #   * full wanted substring is present (ideal), OR
            #   * ≥ half the chars in wanted appear in header_text
            # AND at least one non-space char overlaps.
            # Combined with nav_chat_name (which itself clicked a chat-
            # list block matching wanted), this keeps the wrong-chat
            # defense without false-rejecting Chinese near-matches.
            wlow = wanted.casefold()
            hlow = header_text.casefold()
            if wlow in hlow:
                match_kind = "exact_substring"
            else:
                w_chars = {c for c in wlow if not c.isspace()}
                overlap = w_chars & set(hlow)
                if w_chars and len(overlap) / len(w_chars) >= 0.5:
                    match_kind = (
                        f"partial_chars({len(overlap)}/{len(w_chars)})"
                    )
                else:
                    match_kind = None
            if match_kind is None:
                # Compose a verification screenshot so the agent can see
                # what the header actually said.
                hdr_path: str | None = None
                try:
                    import mss
                    self._screenshot_dir.mkdir(parents=True, exist_ok=True)
                    out = self._screenshot_dir / (
                        f"{int(time.time())}_{call.id[:8]}_header.png"
                    )
                    hx, hy, hw, hh = header_bbox
                    with mss.mss() as sct:
                        grab = sct.grab({
                            "left": hx, "top": hy,
                            "width": hw, "height": hh,
                        })
                        mss.tools.to_png(grab.rgb, grab.size, output=str(out))
                    hdr_path = str(out)
                except Exception:  # noqa: BLE001
                    pass
                err_payload = {
                    "sent": False,
                    "aborted": "wrong_chat",
                    "wanted": wanted,
                    "header_text_seen": header_text[:200],
                    "header_bbox": header_bbox,
                }
                return ToolResult(
                    call_id=call.id, ok=False, content=None,
                    error=(
                        f"verify_chat_title mismatch — wanted "
                        f"{wanted!r}, header OCR says "
                        f"{header_text[:120]!r}. Aborted to prevent "
                        f"sending to the wrong chat. Re-navigate "
                        f"(e.g. click_on_text with the group name) "
                        f"and retry, or inspect the attached header "
                        f"screenshot."
                    ),
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    metadata=(
                        {"attach_image": hdr_path} if hdr_path else {}
                    ),
                )

        # ── Step 2: determine input box coordinate ──
        if isinstance(explicit_bbox, list) and len(explicit_bbox) == 4:
            try:
                ix, iy, iw, ih = (int(v) for v in explicit_bbox)
                click_x = ix + iw // 2
                click_y = iy + ih // 2
                source = "explicit_bbox"
            except (TypeError, ValueError):
                return _fail(
                    call, t0,
                    "input_bbox must be [x, y, w, h] of integers",
                )
        elif target_bbox is not None and target_bbox[2] > 0:
            wx, wy, ww, wh = target_bbox
            # Click x: center the conversation pane (right ~67% of
            # window). Window-center clicks the boundary between chat
            # list and conversation; better to shift right.
            click_x = wx + (ww * 2) // 3
            # Click y: WeChat 4.x layout (validated 2026-05-13):
            #   bottom 50 px = icon row (emoji / file / 截图 / audio)
            #   above icons: ~200 px input typing area
            #   above input: messages
            # CLICKING IN THE ICON ROW triggers the 截图 (screenshot)
            # tool, which captures a region and auto-sends it. Real
            # bug observed: agent's click at wh-70 hit 截图, screenshot
            # got sent instead of the typed text. Fix: click ~150 px
            # above bottom — center of input typing area, well above
            # icons.
            click_y = wy + wh - 150
            source = "window_bottom_heuristic"
        else:
            return _fail(
                call, t0,
                "no input_bbox and no focused window with bbox — "
                "either pass window_title to focus, OR pass "
                "input_bbox=[x,y,w,h] of the input field",
            )

        # ── Step 3: click input box, type via clipboard, press ──
        # Clipboard + Ctrl+V is dramatically more reliable than
        # pyautogui.write for Chinese / IME input. pyautogui's per-
        # key typewriter path hits IME composition quirks on Windows
        # and frequently drops characters mid-word.
        try:
            pg = self._require_pyautogui()
        except ImportError as exc:
            return _fail(call, t0, _pg_install_hint(exc))
        typing_path = "clipboard_paste"
        try:
            await asyncio.to_thread(pg.click, click_x, click_y)
            await asyncio.sleep(0.2)
            try:
                import pyperclip  # type: ignore
                # Save the user's current clipboard so we don't clobber
                # whatever they had on it.
                try:
                    saved_clip = pyperclip.paste()
                except Exception:  # noqa: BLE001
                    saved_clip = None
                await asyncio.to_thread(pyperclip.copy, text)
                await asyncio.sleep(0.1)
                await asyncio.to_thread(pg.hotkey, "ctrl", "v")
                await asyncio.sleep(0.1)
                # Best-effort restore so the user's clipboard isn't
                # surprised after the action.
                if saved_clip is not None:
                    try:
                        await asyncio.to_thread(pyperclip.copy, saved_clip)
                    except Exception:  # noqa: BLE001
                        pass
            except ImportError:
                # Fallback when pyperclip isn't installed.
                typing_path = "pyautogui_write"
                await asyncio.to_thread(pg.write, text, interval=0.01)
            await asyncio.sleep(0.2)
            if press_after.strip():
                pk = press_after.strip().lower()
                if "+" in pk:
                    parts = [p.strip() for p in pk.split("+") if p.strip()]
                    await asyncio.to_thread(pg.hotkey, *parts)
                else:
                    await asyncio.to_thread(pg.press, pk)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                call, t0,
                f"compose-and-send failed at action step: "
                f"{type(exc).__name__}: {exc}",
            )

        payload: dict[str, Any] = {
            "sent": True,
            "click_coordinate": [click_x, click_y],
            "click_source": source,
            "chars_typed": len(text),
            "typing_path": typing_path,
            "pressed": press_after if press_after.strip() else None,
            "window_focused": (
                window_title.strip()
                if isinstance(window_title, str) else None
            ),
            "window_process": chosen_proc_name,
            "window_bbox": target_bbox,
            "nav_chat_name": (
                nav_chat_name.strip()
                if isinstance(nav_chat_name, str) and nav_chat_name.strip()
                else None
            ),
            "nav_clicked": nav_clicked,
            "nav_strategy": nav_strategy,
            "verified_chat_title": (
                verify_chat_title.strip()
                if isinstance(verify_chat_title, str) and verify_chat_title.strip()
                else None
            ),
        }

        if not confirm_screenshot:
            return _ok(call, t0, json.dumps(payload, ensure_ascii=False))

        # Optional verification screenshot via existing screenshot pipeline.
        try:
            import mss
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{int(time.time())}_{call.id[:8]}_verify.png"
            out = self._screenshot_dir / fname

            def _do_capture() -> tuple[int, int]:
                with mss.mss() as sct:
                    grab = sct.grab(sct.monitors[1])
                    mss.tools.to_png(grab.rgb, grab.size, output=str(out))
                    return grab.size

            size = await asyncio.to_thread(_do_capture)
            payload["verify_screenshot_path"] = str(out)
            payload["verify_screenshot_size"] = [int(size[0]), int(size[1])]
            return ToolResult(
                call_id=call.id, ok=True,
                content=json.dumps(payload, ensure_ascii=False),
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                metadata={"attach_image": str(out)},
            )
        except Exception:  # noqa: BLE001 — verification is optional
            payload["verify_screenshot_path"] = None
            return _ok(call, t0, json.dumps(payload, ensure_ascii=False))


# ── Helpers ───────────────────────────────────────────────────────────


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# Phase 9 M2.1: 进程级 DPI 感知（Windows）。不开的话,Windows 显示缩放
# (125%/150%) 下 mss 截的是物理像素、pyautogui 用的是逻辑坐标 —— 模型
# 从截图里读出的坐标点下去会按缩放比例偏移（经典"点不准"根因）。开了
# 之后两边都是物理像素,坐标系对齐。幂等;非 Windows no-op;失败静默
# （screen_capture 会回报 click_scale 让模型自行换算,双保险）。
_dpi_aware_attempted = False


def _ensure_dpi_aware() -> None:
    global _dpi_aware_attempted
    if _dpi_aware_attempted or platform.system() != "Windows":
        _dpi_aware_attempted = True
        return
    _dpi_aware_attempted = True
    try:
        import ctypes
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001 — older Windows / already set
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


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


def _json_content(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return raw
    return raw


def _sha256_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _release_topmost(window: Any) -> bool:
    """Reverse the HWND_TOPMOST pin from ``_force_foreground``.

    Call from a try/finally so the window doesn't stay pinned topmost
    after the GUI operation completes. Returns True on success, False
    on any failure (caller can ignore — leaving a window topmost is
    annoying but not destructive).
    """
    try:
        import ctypes
        hwnd = int(
            getattr(window, "_hWnd", None) or getattr(window, "hWnd", 0)
        )
        if not hwnd:
            return False
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        return bool(ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags,
        ))
    except Exception:  # noqa: BLE001
        return False


def _window_process_name(window: Any) -> str:
    """Return the process executable name (e.g. 'Weixin.exe') for a
    pygetwindow Window. Returns '' on any failure.

    Used by `_pick_chat_window` to distinguish e.g. the real WeChat
    main window ('Weixin.exe') from the WeChat extension/mini-program
    auxiliary window ('WeChatAppEx.exe') and from a browser tab whose
    title also contains '微信'.
    """
    try:
        import ctypes
        import ctypes.wintypes
        hwnd = int(
            getattr(window, "_hWnd", None) or getattr(window, "hWnd", 0)
        )
        if not hwnd:
            return ""
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid),
        )
        if not pid.value:
            return ""
        import psutil  # type: ignore
        p = psutil.Process(pid.value)
        return p.name() or ""
    except Exception:  # noqa: BLE001
        return ""


# Process names known to be browser tabs that often steal "微信"
# title from the real WeChat window. If the window's pid matches one
# of these, we skip it as a chat-app candidate.
_BROWSER_EXE_NAMES = frozenset({
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe", "iexplore.exe", "safari.exe",
})

# WeChat / 飞书 / Slack / Discord / QQ main-app exe names. When
# searching for a chat window by title, we prefer one of these over
# any other process (e.g. WeChat's extension window WeChatAppEx.exe
# which sometimes carries the same '微信' title but is for the
# channels / mini-program viewer, not the main chat surface).
_CHAT_APP_EXE_NAMES = frozenset({
    # WeChat / Tencent
    "weixin.exe",       # WeChat 4.x main
    "wechat.exe",       # WeChat 3.x main
    "qq.exe",
    # ByteDance
    "feishu.exe",
    "lark.exe",
    # International
    "slack.exe",
    "discord.exe",
    "telegram.exe",
})


def _pick_chat_window(title_substring: str) -> Any | None:
    """Find the best matching chat-app window for ``title_substring``.

    Selection logic (per real bug seen 2026-05-13):

    1. Filter to windows whose title contains the substring.
    2. Discard browser-process windows (msedge.exe etc) — their tab
       title can match unrelated text like '微信'.
    3. Discard WeChat extension/auxiliary processes (WeChatAppEx.exe)
       which carry the SAME '微信' title as the main app but are
       full-screen mini-program/channels viewers, not the chat UI.
    4. Prefer windows whose process name is in ``_CHAT_APP_EXE_NAMES``.
    5. Of remaining, prefer NON-minimized and NON-full-screen (the
       main chat window is usually 800×600 or 1300×900, not screen-
       spanning).

    Returns the chosen pygetwindow Window or None.
    """
    try:
        import pygetwindow as gw  # type: ignore
    except ImportError:
        return None
    sub = title_substring.strip().lower()
    if not sub:
        return None
    candidates: list[tuple[Any, str]] = []
    for w in gw.getAllWindows():
        try:
            title = (getattr(w, "title", "") or "").strip()
            if not title or sub not in title.lower():
                continue
            proc = _window_process_name(w).lower()
            if proc in _BROWSER_EXE_NAMES:
                continue
            # WeChatAppEx is the mini-program/channels viewer — NOT
            # the chat surface. Skip even though title matches.
            if proc == "wechatappex.exe":
                continue
            candidates.append((w, proc))
        except Exception:  # noqa: BLE001
            continue
    if not candidates:
        return None
    # Sort: known chat-app exe first, then non-minimized + non-full-
    # screen, then anything else.
    import ctypes
    user32 = ctypes.windll.user32

    def _score(item: tuple[Any, str]) -> tuple[int, int, int]:
        w, proc = item
        in_chat_app = 0 if proc in _CHAT_APP_EXE_NAMES else 1
        is_min = 1 if bool(getattr(w, "isMinimized", False)) else 0
        # Penalize windows that span the full screen — those are
        # usually launchers / fullscreen apps, not chat composers.
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        width = int(getattr(w, "width", 0))
        height = int(getattr(w, "height", 0))
        is_fullscreen = 1 if (width >= sw - 50 and height >= sh - 100) else 0
        return (in_chat_app, is_min, is_fullscreen)

    candidates.sort(key=_score)
    return candidates[0][0]


def _force_foreground(window: Any) -> str:
    """Bring a window to the foreground reliably on Windows.

    pygetwindow's ``activate()`` returns success on Windows even when
    the OS silently denies the focus change due to foreground-lock
    protection (this is what we hit in the WeChat e2e test —
    window_focus said ok but WeChat stayed behind Claude). The
    documented workaround uses the AttachThreadInput technique: we
    attach our thread to the foreground thread, call
    SetForegroundWindow, then detach. This is the same technique
    Microsoft Spy++ and similar tools use.

    Returns the strategy that succeeded so callers / tests can see
    what path was taken. Raises on total failure.

    Windows-only; on other platforms, falls back to plain activate().
    """
    import platform
    if platform.system() != "Windows":
        window.activate()
        return "pygetwindow"

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd_target = int(
        getattr(window, "_hWnd", None) or getattr(window, "hWnd", 0)
    )
    if not hwnd_target:
        window.activate()
        return "pygetwindow"

    # Restore from minimized if needed.
    SW_RESTORE = 9
    if user32.IsIconic(hwnd_target):
        user32.ShowWindow(hwnd_target, SW_RESTORE)

    # BEFORE anything else: pin the window HWND_TOPMOST. This puts
    # it above any other HWND_TOPMOST windows (Claude Code's task
    # panel, system notifications, popup tooltips). DO NOT immediately
    # revert — previous attempts at flip+revert left WeChat back under
    # Claude Code's panel within milliseconds. Caller (gui_send_chat)
    # must call ``_release_topmost(window)`` in a try/finally so the
    # window doesn't stay pinned after the operation.
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2  # noqa: F841 (kept for _release_topmost reference)
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    SWP_NOACTIVATE = 0x0010
    flip_flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd_target, HWND_TOPMOST, 0, 0, 0, 0, flip_flags)

    # Strategy A: bare SetForegroundWindow. Often works when the
    # calling thread is already the foreground thread (rare).
    if user32.SetForegroundWindow(hwnd_target):
        # Confirm.
        if user32.GetForegroundWindow() == hwnd_target:
            return "topmost_flip_set_foreground"

    # Strategy B: AttachThreadInput trick. The classic Windows
    # focus-stealing bypass. Attach our thread to the current
    # foreground thread's input queue, call SetForegroundWindow,
    # detach. Works because Windows trusts focus changes that come
    # from the active thread.
    foreground_hwnd = user32.GetForegroundWindow()
    foreground_tid = user32.GetWindowThreadProcessId(
        foreground_hwnd, None,
    )
    target_tid = user32.GetWindowThreadProcessId(hwnd_target, None)
    current_tid = kernel32.GetCurrentThreadId()

    attached = False
    if foreground_tid and current_tid and foreground_tid != current_tid:
        attached = bool(user32.AttachThreadInput(
            current_tid, foreground_tid, True,
        ))
    try:
        # Also lock + release the lock-set-foreground-window timeout.
        ASFW_ANY = wintypes.DWORD(-1)
        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:  # noqa: BLE001 — some Windows variants lack this
            pass
        # Send a synthetic ALT key — Windows treats this as user input
        # and grants the calling thread foreground rights for the next
        # SetForegroundWindow call.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

        user32.ShowWindow(hwnd_target, SW_RESTORE)
        user32.SetForegroundWindow(hwnd_target)
        # BringWindowToTop forces the Z-order even when foreground
        # isn't granted; combined with SetForegroundWindow above we
        # cover both cases.
        user32.BringWindowToTop(hwnd_target)
    finally:
        if attached:
            user32.AttachThreadInput(current_tid, foreground_tid, False)

    if user32.GetForegroundWindow() == hwnd_target:
        return "attach_thread_input"

    # Strategy C: re-do the TOPMOST flip (we already did it once at
    # the top; doing it again after the AttachThreadInput attempt
    # sometimes shakes loose a stuck z-order).
    user32.SetWindowPos(hwnd_target, HWND_TOPMOST, 0, 0, 0, 0, flip_flags)
    user32.SetWindowPos(hwnd_target, HWND_NOTOPMOST, 0, 0, 0, 0, flip_flags)
    if user32.GetForegroundWindow() == hwnd_target:
        return "topmost_flip_retry"

    # Last-ditch: pygetwindow's activate.
    window.activate()
    if user32.GetForegroundWindow() == hwnd_target:
        return "pygetwindow_fallback"
    raise RuntimeError(
        f"failed to bring window to foreground after all strategies "
        f"(target hwnd {hwnd_target}, current fg "
        f"{user32.GetForegroundWindow()})",
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
    if matches or exact:
        return matches
    # Fuzzy fallback (exact=False only)
    best_ratio = 0.0
    best_block: dict | None = None
    for b in blocks:
        text_norm = b["text"].strip().casefold()
        ratio = difflib.SequenceMatcher(None, text_norm, wanted_norm).ratio()
        if ratio > best_ratio and ratio > 0.65:
            best_ratio = ratio
            best_block = b
    if best_block is not None:
        # Return a shallow copy so we can tag the match type
        tagged = dict(best_block)
        tagged["match_type"] = "fuzzy"
        tagged["fuzzy_ratio"] = round(best_ratio, 4)
        return [tagged]
    return []


__all__ = ["ComputerUseTools"]
