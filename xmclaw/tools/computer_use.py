"""Computer Use tool for desktop automation."""
import base64
import io
from typing import Any

from xmclaw.tools.base import Tool


class ComputerUseTool(Tool):
    name = "computer_use"
    description = (
        "Control the computer desktop: take screenshots, move mouse, click, type text, press keys, "
        "scroll wheel, and drag. Actions: screenshot, click, move, type, key, scroll, drag."
    )

    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["screenshot", "click", "move", "type", "key", "scroll", "drag"],
                        "description": "The action to perform",
                    },
                    "x": {"type": "integer", "description": "X coordinate for click/move/drag"},
                    "y": {"type": "integer", "description": "Y coordinate for click/move/drag"},
                    "end_x": {"type": "integer", "description": "End X coordinate for drag"},
                    "end_y": {"type": "integer", "description": "End Y coordinate for drag"},
                    "text": {"type": "string", "description": "Text to type"},
                    "key": {"type": "string", "description": "Key to press (e.g., enter, ctrl, alt, tab)"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "clicks": {"type": "integer", "default": 1, "description": "Number of clicks"},
                    "scroll_x": {"type": "integer", "default": 0, "description": "Horizontal scroll amount"},
                    "scroll_y": {"type": "integer", "description": "Vertical scroll amount (positive=up, negative=down)"},
                },
                "required": ["action"],
            },
        }

    async def execute(self, action: str, **kwargs) -> str:
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
        except ImportError:
            return "[Error: pyautogui is not installed. Run: pip install pyautogui]"

        action = action.lower()

        if action == "screenshot":
            return self._screenshot()
        elif action == "click":
            x = kwargs.get("x")
            y = kwargs.get("y")
            button = kwargs.get("button", "left")
            clicks = kwargs.get("clicks", 1)
            if x is None or y is None:
                return "[Error: click requires x and y coordinates]"
            pyautogui.click(int(x), int(y), button=button, clicks=int(clicks))
            return f"Clicked {button} {clicks} time(s) at ({x}, {y})"
        elif action == "move":
            x = kwargs.get("x")
            y = kwargs.get("y")
            if x is None or y is None:
                return "[Error: move requires x and y coordinates]"
            pyautogui.moveTo(int(x), int(y))
            return f"Moved mouse to ({x}, {y})"
        elif action == "type":
            text = kwargs.get("text", "")
            pyautogui.typewrite(text, interval=0.01)
            return f"Typed: {text[:50]}"
        elif action == "key":
            key = kwargs.get("key", "")
            pyautogui.press(key)
            return f"Pressed key: {key}"
        elif action == "scroll":
            scroll_y = kwargs.get("scroll_y", 0)
            scroll_x = kwargs.get("scroll_x", 0)
            x = kwargs.get("x")
            y = kwargs.get("y")
            if x is not None and y is not None:
                pyautogui.scroll(int(scroll_y), int(x), int(y))
                return f"Scrolled ({scroll_x}, {scroll_y}) at ({x}, {y})"
            pyautogui.hscroll(int(scroll_x))
            pyautogui.scroll(int(scroll_y))
            return f"Scrolled ({scroll_x}, {scroll_y})"
        elif action == "drag":
            x = kwargs.get("x")
            y = kwargs.get("y")
            end_x = kwargs.get("end_x")
            end_y = kwargs.get("end_y")
            button = kwargs.get("button", "left")
            if x is None or y is None or end_x is None or end_y is None:
                return "[Error: drag requires x, y, end_x, and end_y coordinates]"
            pyautogui.moveTo(int(x), int(y))
            pyautogui.dragTo(int(end_x), int(end_y), button=button)
            return f"Dragged {button} from ({x}, {y}) to ({end_x}, {end_y})"
        else:
            return f"[Error: Unknown action '{action}']"

    def _screenshot(self) -> str:
        try:
            import mss
        except ImportError:
            return "[Error: mss is not installed. Run: pip install mss]"

        with mss.mss() as sct:
            monitor = sct.monitors[0]  # Full screen
            img = sct.grab(monitor)
            from PIL import Image
            pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
            buffer = io.BytesIO()
            pil_img.save(buffer, format="PNG")
            b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
            return f"data:image/png;base64,{b64}"
