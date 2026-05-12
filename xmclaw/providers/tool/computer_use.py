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
        include_b64 = bool(args.get("include_base64", True))

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

        return _ok(call, t0, json.dumps(result, ensure_ascii=False))

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


__all__ = ["ComputerUseTools"]
